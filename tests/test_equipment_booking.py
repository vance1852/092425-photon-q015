from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from equipment_booking.clock import FrozenClock
from equipment_booking.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from equipment_booking.service import BookingService
from equipment_booking.storage import connect

FROZEN = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)  # UTC 周一 08:00


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(FROZEN)
        self.service = BookingService(self.connection, self.clock)
        self.service.bootstrap_admin("admin", "team-ops")
        self.service.create_team("admin", "team-a", "光电器件组", weekly_quota_minutes=180)
        self.service.create_team("admin", "team-b", "先进封测组", weekly_quota_minutes=240)
        self.service.create_resource("admin", "spec-1", "共享光谱仪", "spectrometer")
        self.service.create_resource("admin", "line-1", "共享封测线", "packaging")
        self.service.create_user("admin", "alice", "Alice", "team-a", "engineer")
        self.service.create_user("admin", "bob", "Bob", "team-b", "engineer")

    def tearDown(self) -> None:
        self.connection.close()

    def book(self, actor="alice", resource="spec-1", start="2026-09-21T10:00:00Z",
             end="2026-09-21T11:00:00Z", purpose="光谱测量", **kwargs):
        return self.service.create_reservation(actor, resource, start, end, purpose, **kwargs)


class ResourceTeamTests(ServiceFixture):
    def test_only_admin_manages_teams_and_resources(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_team("alice", "team-c", "X", 60)
        with self.assertRaises(Forbidden):
            self.service.create_resource("alice", "spec-2", "X", "spectrometer")
        with self.assertRaises(Forbidden):
            self.service.set_weekly_quota("alice", "team-a", 999)
        team = self.service.set_weekly_quota("admin", "team-a", 300)
        self.assertEqual(team["weekly_quota_minutes"], 300)

    def test_quota_change_is_audited(self) -> None:
        self.service.set_weekly_quota("admin", "team-a", 300)
        events = [e for e in self.service.audit_events("admin", "team", "team-a")
                  if e["event_type"] == "team.quota_changed"]
        self.assertEqual(len(events), 1)
        self.assertIn("new_weekly_quota_minutes", events[0]["payload_json"])

    def test_unknown_actor_and_resource(self) -> None:
        with self.assertRaises(NotFound):
            self.service.resources("nobody")
        with self.assertRaises(NotFound):
            self.service.resource("alice", "missing")


class ReservationRuleTests(ServiceFixture):
    def test_engineer_books_only_own_team(self) -> None:
        rsv = self.book()
        self.assertEqual(rsv["team_id"], "team-a")
        self.assertEqual(rsv["status"], "booked")
        with self.assertRaises(Forbidden):
            self.book(actor="bob", team_id="team-a")
        # 管理员可以代任意团队预约。
        admin_rsv = self.book(actor="admin", start="2026-09-21T12:00:00Z",
                              end="2026-09-21T13:00:00Z", team_id="team-b")
        self.assertEqual(admin_rsv["team_id"], "team-b")

    def test_overlap_rejected_but_adjacent_allowed(self) -> None:
        self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T11:30:00Z")
        with self.assertRaises(Conflict):
            self.book(actor="bob", start="2026-09-21T11:00:00Z", end="2026-09-21T12:00:00Z")
        # 半开区间：11:30 首尾相接不冲突。
        adjacent = self.book(actor="bob", start="2026-09-21T11:30:00Z",
                             end="2026-09-21T12:30:00Z")
        self.assertEqual(adjacent["status"], "booked")
        # 已取消的预约不再阻塞同一时段。
        self.service.cancel_reservation("bob", adjacent["reservation_id"], "计划变更")
        again = self.book(actor="bob", start="2026-09-21T11:30:00Z",
                          end="2026-09-21T12:00:00Z")
        self.assertEqual(again["status"], "booked")

    def test_different_resources_do_not_conflict(self) -> None:
        self.book(resource="spec-1")
        other = self.book(resource="line-1")
        self.assertEqual(other["resource_id"], "line-1")

    def test_cannot_book_in_the_past(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.book(start="2026-09-21T07:00:00Z", end="2026-09-21T07:30:00Z")

    def test_naive_timestamp_rejected_offset_normalized(self) -> None:
        with self.assertRaises(ValueError):
            self.book(start="2026-09-21T18:00:00", end="2026-09-21T19:00:00")
        # 北京时间 18:00 == UTC 10:00，归一化后与既有预约冲突。
        self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T11:00:00Z")
        with self.assertRaises(Conflict):
            self.book(actor="bob", start="2026-09-21T18:00:00+08:00",
                      end="2026-09-21T19:00:00+08:00")


class QuotaTests(ServiceFixture):
    def test_weekly_quota_blocks_and_releases(self) -> None:
        self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T12:30:00Z")  # 150 分钟
        with self.assertRaises(Conflict):
            self.book(start="2026-09-21T14:00:00Z", end="2026-09-21T15:00:00Z")  # +60
        rsv = self.book(start="2026-09-21T14:00:00Z", end="2026-09-21T14:30:00Z")  # +30
        usage = self.service.weekly_quota_usage("alice", "team-a", "2026-W39")
        self.assertEqual(usage["booked_minutes"], 180)
        self.assertEqual(usage["remaining_minutes"], 0)
        self.service.cancel_reservation("alice", rsv["reservation_id"], "释放配额")
        usage = self.service.weekly_quota_usage("alice", "team-a", "2026-W39")
        self.assertEqual(usage["booked_minutes"], 150)
        self.assertEqual(usage["remaining_minutes"], 30)

    def test_quota_is_charged_per_utc_week_for_spanning_reservation(self) -> None:
        self.service.set_weekly_quota("admin", "team-a", 90)
        # 周日 23:00 → 周一 01:00：跨 W39/W40 两周，各计 60 分钟。
        self.book(start="2026-09-27T23:00:00Z", end="2026-09-28T01:00:00Z")
        self.assertEqual(self.service.weekly_quota_usage("alice", "team-a", "2026-W39")["booked_minutes"], 60)
        self.assertEqual(self.service.weekly_quota_usage("alice", "team-a", "2026-W40")["booked_minutes"], 60)
        # W40 再订 60 分钟会达到 120，超过 90。
        with self.assertRaises(Conflict):
            self.book(start="2026-09-28T02:00:00Z", end="2026-09-28T03:00:00Z")
        # W39 还剩 30 分钟，可以订。
        more = self.book(start="2026-09-27T22:00:00Z", end="2026-09-27T22:30:00Z")
        self.assertTrue(more["reservation_id"].startswith("rsv-"))

    def test_engineer_cannot_read_other_team_quota(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.weekly_quota_usage("alice", "team-b")

    def test_zero_quota_means_unlimited(self) -> None:
        self.service.set_weekly_quota("admin", "team-a", 0)
        self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T20:00:00Z")
        usage = self.service.weekly_quota_usage("alice", "team-a")
        self.assertTrue(usage["unlimited"])
        self.assertIsNone(usage["remaining_minutes"])


class CancelRescheduleTests(ServiceFixture):
    def test_team_membership_required_for_cancel_and_reschedule(self) -> None:
        rsv = self.book(actor="alice")
        with self.assertRaises(Forbidden):
            self.service.cancel_reservation("bob", rsv["reservation_id"], "替人取消")
        with self.assertRaises(Forbidden):
            self.service.reschedule_reservation(
                "bob", rsv["reservation_id"], "2026-09-21T12:00:00Z", "2026-09-21T13:00:00Z")
        with self.assertRaises(Forbidden):
            self.service.reservation("bob", rsv["reservation_id"])

    def test_double_cancel_rejected(self) -> None:
        rsv = self.book()
        self.service.cancel_reservation("alice", rsv["reservation_id"], "第一次")
        with self.assertRaises(InvalidState):
            self.service.cancel_reservation("alice", rsv["reservation_id"], "第二次")
        with self.assertRaises(InvalidState):
            self.service.reschedule_reservation(
                "alice", rsv["reservation_id"], "2026-09-21T12:00:00Z", "2026-09-21T13:00:00Z")

    def test_started_reservation_is_locked_for_engineers(self) -> None:
        rsv = self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T11:00:00Z")
        self.clock.advance(hours=2, minutes=1)
        with self.assertRaises(Forbidden):
            self.service.cancel_reservation("alice", rsv["reservation_id"], "已经开始了")
        with self.assertRaises(Forbidden):
            self.service.reschedule_reservation(
                "alice", rsv["reservation_id"], "2026-09-22T10:00:00Z", "2026-09-22T11:00:00Z")
        forced = self.service.cancel_reservation("admin", rsv["reservation_id"], "设备检修")
        self.assertEqual(forced["status"], "cancelled")

    def test_reschedule_checks_conflict_quota_and_keeps_audit(self) -> None:
        first = self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T11:30:00Z")
        self.book(actor="bob", start="2026-09-21T13:00:00Z", end="2026-09-21T14:00:00Z")
        with self.assertRaises(Conflict):
            self.service.reschedule_reservation(
                "alice", first["reservation_id"],
                "2026-09-21T13:30:00Z", "2026-09-21T14:30:00Z")
        moved = self.service.reschedule_reservation(
            "alice", first["reservation_id"],
            "2026-09-21T15:00:00Z", "2026-09-21T15:30:00Z", purpose="改期后的用途")
        self.assertEqual(moved["revision"], 2)
        event = [e for e in self.service.audit_events("admin", "reservation", first["reservation_id"])
                 if e["event_type"] == "reservation.rescheduled"][0]
        self.assertIn("before", event["payload_json"])
        self.assertIn("after", event["payload_json"])


class IdempotencyTests(ServiceFixture):
    def test_replay_returns_same_reservation(self) -> None:
        kwargs = dict(start="2026-09-21T10:00:00Z", end="2026-09-21T11:00:00Z",
                      idempotency_key="alice-key-1")
        first = self.book(**kwargs)
        second = self.book(**kwargs)
        self.assertEqual(first["reservation_id"], second["reservation_id"])
        rows = self.connection.execute("SELECT count(*) FROM reservations").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_same_key_different_payload_is_conflict(self) -> None:
        self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T11:00:00Z",
                  idempotency_key="key-x")
        with self.assertRaises(Conflict):
            self.book(start="2026-09-21T12:00:00Z", end="2026-09-21T13:00:00Z",
                      idempotency_key="key-x")


class ScheduleAuditTests(ServiceFixture):
    def test_schedule_window_filters(self) -> None:
        keep = self.book(start="2026-09-21T10:00:00Z", end="2026-09-21T11:00:00Z")
        self.book(start="2026-09-23T10:00:00Z", end="2026-09-23T11:00:00Z")
        view = self.service.schedule(
            "alice", "spec-1", "2026-09-21T09:00:00Z", "2026-09-21T12:00:00Z")
        self.assertEqual([r["reservation_id"] for r in view["reservations"]], [keep["reservation_id"]])

    def test_audit_chain_records_every_change_and_detects_tampering(self) -> None:
        rsv = self.book()
        self.service.reschedule_reservation(
            "alice", rsv["reservation_id"], "2026-09-21T15:00:00Z", "2026-09-21T15:30:00Z")
        self.service.cancel_reservation("alice", rsv["reservation_id"], "不再需要")
        chain = self.service.verify_chain("admin")
        self.assertTrue(chain["valid"])
        self.assertGreaterEqual(chain["events"], 3)
        types = [e["event_type"] for e in
                 self.service.audit_events("admin", "reservation", rsv["reservation_id"])]
        self.assertEqual(
            types,
            ["reservation.created", "reservation.rescheduled", "reservation.cancelled"],
        )
        # 直接篡改业务表外的审计载荷必须能被发现。
        self.connection.execute(
            "UPDATE reservation_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.verify_chain("admin")["valid"])

    def test_audit_requires_admin(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.audit_events("alice")
        with self.assertRaises(Forbidden):
            self.service.verify_chain("alice")


class ConcurrentBookingTests(unittest.TestCase):
    """真实线程 + 文件数据库：并发抢同一时段必须恰好一个成功。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "booking.sqlite3")
        admin_db = connect(self.path)
        service = BookingService(admin_db, FrozenClock(FROZEN))
        service.bootstrap_admin("admin", "team-ops")
        service.create_team("admin", "team-a", "光电器件组", 0)
        service.create_resource("admin", "spec-1", "共享光谱仪", "spectrometer")
        service.create_user("admin", "a1", "A1", "team-a", "engineer")
        service.create_user("admin", "a2", "A2", "team-a", "engineer")
        admin_db.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_exactly_one_winner_among_concurrent_overlaps(self) -> None:
        results: list[object] = []
        barrier = threading.Barrier(2)

        def attempt(actor: str) -> None:
            db = connect(self.path)
            svc = BookingService(db, FrozenClock(FROZEN))
            barrier.wait()
            try:
                rsv = svc.create_reservation(
                    actor, "spec-1",
                    "2026-09-21T10:00:00Z", "2026-09-21T11:00:00Z", "并发抢约",
                )
                results.append(("ok", rsv["reservation_id"]))
            except Conflict as exc:
                results.append(("conflict", str(exc)))
            finally:
                db.close()

        threads = [threading.Thread(target=attempt, args=(actor,)) for actor in ("a1", "a2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(status for status, _ in results), ["conflict", "ok"])
        db = connect(self.path)
        count = db.execute(
            "SELECT count(*) FROM reservations WHERE status='booked'").fetchone()[0]
        self.assertEqual(count, 1)
        db.close()

    def test_concurrent_partial_overlap_has_one_winner(self) -> None:
        """起始时刻不同但区间重叠：唯一索引兜不住，靠事务串行化保证唯一成功。"""
        results: list[object] = []
        barrier = threading.Barrier(2)

        def attempt(actor: str, start: str, end: str) -> None:
            db = connect(self.path)
            svc = BookingService(db, FrozenClock(FROZEN))
            barrier.wait()
            try:
                rsv = svc.create_reservation(actor, "spec-1", start, end, "部分重叠")
                results.append(("ok", rsv["reservation_id"]))
            except Conflict:
                results.append(("conflict", None))
            finally:
                db.close()

        threads = [
            threading.Thread(target=attempt, args=("a1", "2026-09-21T10:00:00Z", "2026-09-21T11:00:00Z")),
            threading.Thread(target=attempt, args=("a2", "2026-09-21T10:30:00Z", "2026-09-21T11:30:00Z")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sorted(status for status, _ in results), ["conflict", "ok"])
        db = connect(self.path)
        self.assertEqual(
            db.execute("SELECT count(*) FROM reservations WHERE status='booked'").fetchone()[0], 1)
        db.close()

    def test_concurrent_non_overlapping_both_succeed(self) -> None:
        outcomes: list[bool] = []
        barrier = threading.Barrier(2)

        def attempt(actor: str, start: str, end: str) -> None:
            db = connect(self.path)
            svc = BookingService(db, FrozenClock(FROZEN))
            barrier.wait()
            try:
                svc.create_reservation(actor, "spec-1", start, end, "不重叠")
                outcomes.append(True)
            except Conflict:
                outcomes.append(False)
            finally:
                db.close()

        threads = [
            threading.Thread(target=attempt, args=("a1", "2026-09-21T10:00:00Z", "2026-09-21T11:00:00Z")),
            threading.Thread(target=attempt, args=("a2", "2026-09-21T11:00:00Z", "2026-09-21T12:00:00Z")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sorted(outcomes), [True, True])


if __name__ == "__main__":
    unittest.main()
