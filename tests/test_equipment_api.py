from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from equipment_booking.api import JsonApplication
from equipment_booking.clock import FrozenClock
from equipment_booking.service import BookingService


def headers(actor: str | None = None) -> dict[str, str]:
    return {"X-Actor-Id": actor} if actor else {}


class ApiFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))
        self.service = BookingService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        self.service.bootstrap_admin("admin", "team-ops")

    def tearDown(self) -> None:
        self.connection.close()

    def call(self, method: str, target: str, actor: str | None = "admin", body: dict | None = None):
        payload = json.dumps(body).encode() if body is not None else b""
        return self.app.handle(method, target, headers(actor), payload)


class ApiTests(ApiFixture):
    def test_health_and_auth_required(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/teams")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.body["error"]["code"], "unauthenticated")

    def test_full_flow_over_http(self) -> None:
        r = self.call("POST", "/teams", body={"team_id": "team-a", "name": "器件组",
                                              "weekly_quota_minutes": 120})
        self.assertEqual(r.status, 201)
        r = self.call("POST", "/resources", body={"resource_id": "spec-1", "name": "光谱仪",
                                                  "kind": "spectrometer"})
        self.assertEqual(r.status, 201)
        r = self.call("POST", "/users", body={"user_id": "alice", "display_name": "Alice",
                                              "team_id": "team-a", "role": "engineer"})
        self.assertEqual(r.status, 201)

        r = self.call("POST", "/reservations", actor="alice", body={
            "resource_id": "spec-1",
            "starts_at": "2026-09-21T10:00:00Z",
            "ends_at": "2026-09-21T11:00:00Z",
            "purpose": "扫描",
        })
        self.assertEqual(r.status, 201)
        reservation_id = r.body["reservation_id"]

        r = self.call("POST", "/reservations", actor="alice", body={
            "resource_id": "spec-1",
            "starts_at": "2026-09-21T10:30:00Z",
            "ends_at": "2026-09-21T11:30:00Z",
            "purpose": "重叠",
        })
        self.assertEqual(r.status, 409)
        self.assertEqual(r.body["error"]["code"], "conflict")

        r = self.call("GET", "/teams/team-a/quota?week=2026-W39", actor="alice")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body["booked_minutes"], 60)
        self.assertEqual(r.body["remaining_minutes"], 60)

        r = self.call("GET", "/resources/spec-1/schedule?start=2026-09-21T09:00:00Z&end=2026-09-21T12:00:00Z",
                      actor="alice")
        self.assertEqual(len(r.body["reservations"]), 1)

        r = self.call("POST", f"/reservations/{reservation_id}/cancel", actor="alice",
                      body={"reason": "计划调整"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body["status"], "cancelled")

        r = self.call("GET", "/audit/verify")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.body["valid"])

    def test_forbidden_and_not_found_shapes(self) -> None:
        self.call("POST", "/teams", body={"team_id": "team-a", "name": "器件组"})
        self.call("POST", "/users", body={"user_id": "alice", "display_name": "Alice",
                                          "team_id": "team-a"})
        r = self.call("POST", "/teams", actor="alice", body={"team_id": "team-c", "name": "X"})
        self.assertEqual(r.status, 403)
        self.assertEqual(r.body["error"]["code"], "forbidden")
        r = self.call("GET", "/resources/nope", actor="alice")
        self.assertEqual(r.status, 404)
        self.assertEqual(r.body["error"]["code"], "not_found")

    def test_validation_errors(self) -> None:
        r = self.app.handle("POST", "/teams", headers("admin"), b"not-json")
        self.assertEqual(r.status, 422)
        r = self.call("POST", "/resources", body={"resource_id": "spec-1"})  # 缺字段
        self.assertEqual(r.status, 422)
        r = self.call("PUT", "/teams/team-a/quota", body={"weekly_quota_minutes": -1})
        self.assertEqual(r.status, 422)
        # 不存在的团队返回 404。
        self.call("POST", "/resources", body={"resource_id": "spec-1", "name": "n", "kind": "k"})
        r = self.call("POST", "/users", body={"user_id": "alice", "display_name": "A",
                                              "team_id": "ghost"})
        self.assertEqual(r.status, 404)

    def test_reschedule_and_started_lock(self) -> None:
        self.call("POST", "/teams", body={"team_id": "team-a", "name": "器件组"})
        self.call("POST", "/resources", body={"resource_id": "spec-1", "name": "光谱仪",
                                              "kind": "spectrometer"})
        self.call("POST", "/users", body={"user_id": "alice", "display_name": "Alice",
                                          "team_id": "team-a"})
        r = self.call("POST", "/reservations", actor="alice", body={
            "resource_id": "spec-1", "starts_at": "2026-09-21T10:00:00Z",
            "ends_at": "2026-09-21T11:00:00Z", "purpose": "扫描"})
        reservation_id = r.body["reservation_id"]
        r = self.call("POST", f"/reservations/{reservation_id}/reschedule", actor="alice", body={
            "starts_at": "2026-09-21T15:00:00Z", "ends_at": "2026-09-21T15:30:00Z"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body["revision"], 2)

        self.clock.advance(hours=8)
        r = self.call("POST", f"/reservations/{reservation_id}/cancel", actor="alice",
                      body={"reason": "迟到的取消"})
        self.assertEqual(r.status, 403)
        r = self.call("POST", f"/reservations/{reservation_id}/cancel",
                      body={"reason": "管理员强制取消"})
        self.assertEqual(r.status, 200)

    def test_unknown_route(self) -> None:
        r = self.app.handle("DELETE", "/teams/team-a", headers("admin"))
        self.assertEqual(r.status, 404)
        self.assertEqual(r.body["error"]["code"], "route_not_found")


if __name__ == "__main__":
    unittest.main()
