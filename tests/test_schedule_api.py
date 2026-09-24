from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.client import IncompleteRead
from http.server import ThreadingHTTPServer
from pathlib import Path

from photon_fab import api
from photon_fab.schedule import ScheduleService
from photon_fab.service import PhotonService


class ScheduleHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        db_path = str(Path(self.directory.name) / "photon.sqlite3")
        service = PhotonService(db_path)
        service.bootstrap_admin("admin", "admin-pass-123")
        service.auth.create_user("eng-a", "eng-a-pass-123", "engineer")
        service.auth.create_user("eng-b", "eng-b-pass-123", "engineer")
        schedule = ScheduleService(auth=service.auth)
        admin = service.auth.login("admin", "admin-pass-123")
        schedule.create_team(admin, "team-a", "A 组", 120)
        schedule.create_team(admin, "team-b", "B 组", 120)
        schedule.create_resource(admin, "spec-1", "光谱仪 1", "spectrometer")
        schedule.add_member(admin, "eng-a", "team-a")
        schedule.add_member(admin, "eng-b", "team-b")

        api.Handler.service = service
        api.Handler.schedule = schedule
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.eng_a = self._login("eng-a", "eng-a-pass-123")
        self.eng_b = self._login("eng-b", "eng-b-pass-123")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def _request(self, method: str, path: str, token: str | None = None, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _login(self, user_id: str, password: str) -> str:
        status, body = self._request("POST", "/login", body={"user_id": user_id, "password": password})
        self.assertEqual(status, 200)
        return body["token"]

    def test_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_create_and_conflict_returns_409(self) -> None:
        payload = {
            "resource_id": "spec-1",
            "starts_at": "2026-09-28T01:00:00Z",
            "ends_at": "2026-09-28T02:00:00Z",
        }
        status, first = self._request("POST", "/bookings", self.eng_a, payload)
        self.assertEqual(status, 201)
        status, body = self._request("POST", "/bookings", self.eng_b, payload)
        self.assertEqual(status, 409)
        self.assertIn("already booked", body["error"])

    def test_forbidden_team_and_quota_endpoint(self) -> None:
        status, body = self._request(
            "POST", "/bookings", self.eng_a,
            {
                "resource_id": "spec-1",
                "team_id": "team-b",
                "starts_at": "2026-09-28T01:00:00Z",
                "ends_at": "2026-09-28T02:00:00Z",
            },
        )
        self.assertEqual(status, 403)
        status, body = self._request("GET", "/quota?team_id=team-b", self.eng_a)
        self.assertEqual(status, 403)

    def test_cancel_flow_and_audit(self) -> None:
        _, booking = self._request(
            "POST", "/bookings", self.eng_a,
            {
                "resource_id": "spec-1",
                "starts_at": "2026-09-28T03:00:00Z",
                "ends_at": "2026-09-28T04:00:00Z",
            },
        )
        status, body = self._request(
            "POST", f"/bookings/{booking['booking_id']}/cancel", self.eng_a, {"reason": "调整"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "cancelled")
        status, body = self._request(
            "GET", f"/audit?entity_type=booking&entity_id={booking['booking_id']}", self.eng_a
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["events"][0]["action"], "booking.cancelled")


if __name__ == "__main__":
    unittest.main()
