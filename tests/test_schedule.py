from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from photon_fab.auth import Auth
from photon_fab.errors import Conflict, InvalidState, QuotaExceeded
from photon_fab.schedule import ScheduleService
from photon_fab.storage_schedule import connect


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        from datetime import timedelta

        self.value += timedelta(**kwargs)


class ScheduleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = connect(":memory:")
        self.auth = Auth(self.db)
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.svc = ScheduleService(auth=self.auth, clock=self.clock)
        self.auth.create_user("admin1", "admin-pass-123", "admin")
        self.auth.create_user("eng-a", "eng-a-pass-123", "engineer")
        self.auth.create_user("eng-b", "eng-b-pass-123", "engineer")
        self.auth.create_user("op1", "op-pass-12345", "operator")
        self.admin = self.auth.login("admin1", "admin-pass-123")
        self.eng_a = self.auth.login("eng-a", "eng-a-pass-123")
        self.eng_b = self.auth.login("eng-b", "eng-b-pass-123")
        self.op = self.auth.login("op1", "op-pass-12345")
        self.svc.create_team(self.admin, "team-a", "A 组", weekly_quota_minutes=120)
        self.svc.create_team(self.admin, "team-b", "B 组", weekly_quota_minutes=120)
        self.svc.create_resource(self.admin, "spec-1", "光谱仪 1", "spectrometer")
        self.svc.create_resource(self.admin, "line-1", "封测线 1", "packaging_line")
        self.svc.add_member(self.admin, "eng-a", "team-a")
        self.svc.add_member(self.admin, "eng-b", "team-b")

    def tearDown(self) -> None:
        self.db.close()


class TeamAndResourceTests(ScheduleTestBase):
    def test_admin_only_management(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.create_team(self.eng_a, "team-c", "C 组")
        with self.assertRaises(PermissionError):
            self.svc.create_resource(self.eng_a, "spec-2", "光谱仪 2", "spectrometer")
        with self.assertRaises(PermissionError):
            self.svc.set_quota(self.eng_a, "team-a", 999)

    def test_quota_change_is_audited(self) -> None:
        self.svc.set_quota(self.admin, "team-a", 300)
        team = self.svc.get_team(self.admin, "team-a")
        self.assertEqual(team["weekly_quota_minutes"], 300)
        events = self.svc.audit_events(self.admin, entity_type="team", entity_id="team-a")
        actions = [e["action"] for e in events]
        self.assertIn("quota.changed", actions)
        changed = next(e for e in events if e["action"] == "quota.changed")
        self.assertEqual(changed["payload"], {"old_minutes": 120, "new_minutes": 300})

    def test_bad_resource_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.create_resource(self.admin, "x", "X", "microscope")


class BookingTests(ScheduleTestBase):
    SLOT_A = ("2026-09-28T01:00:00Z", "2026-09-28T02:00:00Z")
    SLOT_B = ("2026-09-28T02:00:00Z", "2026-09-28T03:00:00Z")

    def test_engineer_books_for_own_team(self) -> None:
        booking = self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A)
        self.assertEqual(booking["team_id"], "team-a")
        self.assertEqual(booking["state"], "booked")
        self.assertEqual(booking["duration_minutes"], 60)

    def test_operator_cannot_book(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.create_booking(self.op, "spec-1", *self.SLOT_A)

    def test_cannot_book_for_other_team(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A, team_id="team-b")

    def test_overlap_conflict_but_back_to_back_ok(self) -> None:
        self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A)
        # 首尾相接（半开区间）允许
        self.svc.create_booking(self.eng_b, "spec-1", *self.SLOT_B)
        # 部分重叠拒绝
        with self.assertRaises(Conflict):
            self.svc.create_booking(
                self.eng_a, "spec-1", "2026-09-28T02:30:00Z", "2026-09-28T03:30:00Z"
            )
        # 同一时段不同资源允许
        other = self.svc.create_booking(self.eng_b, "line-1", *self.SLOT_A)
        self.assertEqual(other["resource_id"], "line-1")

    def test_cancelled_booking_frees_slot(self) -> None:
        booking = self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A)
        with self.assertRaises(Conflict):
            self.svc.create_booking(self.eng_b, "spec-1", *self.SLOT_A)
        self.svc.cancel_booking(self.eng_a, booking["booking_id"], "计划取消")
        replacement = self.svc.create_booking(self.eng_b, "spec-1", *self.SLOT_A)
        self.assertEqual(replacement["team_id"], "team-b")

    def test_cannot_cancel_other_teams_booking(self) -> None:
        booking = self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A)
        with self.assertRaises(PermissionError):
            self.svc.cancel_booking(self.eng_b, booking["booking_id"], "抢占")

    def test_started_booking_locked_for_engineer_admin_can_override(self) -> None:
        booking = self.svc.create_booking(
            self.eng_a, "spec-1", "2026-09-24T09:00:00Z", "2026-09-24T10:00:00Z"
        )
        # 开始前工程师可以取消
        self.clock.advance(hours=0)  # 仍是 08:00
        self.assertEqual(booking["starts_at"], "2026-09-24T09:00:00+00:00")
        future = self.svc.create_booking(
            self.eng_a, "spec-1", "2026-09-24T11:00:00Z", "2026-09-24T12:00:00Z"
        )
        self.clock.advance(hours=3, minutes=30)  # 11:30，两个预约均已开始
        with self.assertRaises(InvalidState):
            self.svc.cancel_booking(self.eng_a, booking["booking_id"], "迟到的取消")
        with self.assertRaises(InvalidState):
            self.svc.change_booking(
                self.eng_a, future["booking_id"],
                "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z",
            )
        # 管理员仍可改写
        changed = self.svc.change_booking(
            self.admin, future["booking_id"],
            "2026-09-25T11:00:00Z", "2026-09-25T12:00:00Z",
        )
        self.assertEqual(changed["revision"], 2)
        cancelled = self.svc.cancel_booking(self.admin, booking["booking_id"], "管理员处置")
        self.assertEqual(cancelled["state"], "cancelled")

    def test_change_rechecks_conflict_and_quota(self) -> None:
        first = self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A)
        self.svc.create_booking(self.eng_b, "spec-1", *self.SLOT_B)
        with self.assertRaises(Conflict):
            self.svc.change_booking(
                self.eng_a, first["booking_id"],
                "2026-09-28T02:30:00Z", "2026-09-28T03:30:00Z",
            )

    def test_cancel_and_change_keep_audit(self) -> None:
        booking = self.svc.create_booking(self.eng_a, "spec-1", *self.SLOT_A)
        self.svc.change_booking(
            self.eng_a, booking["booking_id"],
            "2026-09-28T01:30:00Z", "2026-09-28T02:30:00Z",
        )
        self.svc.cancel_booking(self.eng_a, booking["booking_id"], "不再需要")
        actions = [
            e["action"]
            for e in self.svc.audit_events(self.admin, entity_type="booking", entity_id=booking["booking_id"])
        ]
        self.assertEqual(actions, ["booking.cancelled", "booking.changed", "booking.created"])
        # 取消后默认列表不再包含
        ids = [b["booking_id"] for b in self.svc.list_bookings(self.admin, resource_id="spec-1")]
        self.assertNotIn(booking["booking_id"], ids)
        ids_all = [
            b["booking_id"]
            for b in self.svc.list_bookings(self.admin, resource_id="spec-1", include_cancelled=True)
        ]
        self.assertIn(booking["booking_id"], ids_all)

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.create_booking(self.eng_a, "spec-1", "2026-09-28T01:00:00", "2026-09-28T02:00:00")

    def test_offset_times_normalized_to_utc(self) -> None:
        booking = self.svc.create_booking(
            self.eng_a, "spec-1",
            "2026-09-28T09:00:00+08:00", "2026-09-28T10:00:00+08:00",
        )
        self.assertEqual(booking["starts_at"], "2026-09-28T01:00:00+00:00")


class QuotaTests(ScheduleTestBase):
    def test_quota_enforced_within_week(self) -> None:
        # team-a 配额 120 分钟
        self.svc.create_booking(
            self.eng_a, "spec-1",
            "2026-09-28T00:00:00Z", "2026-09-28T01:30:00Z",
        )
        with self.assertRaises(QuotaExceeded):
            self.svc.create_booking(
                self.eng_a, "line-1",
                "2026-09-28T03:00:00Z", "2026-09-28T04:00:00Z",
            )

    def test_quota_resets_per_week_and_split_booking_charged_each_week(self) -> None:
        # 跨周边界（2026-09-28 是周一 00:00 UTC）：两侧各计 30 分钟
        self.svc.create_booking(
            self.eng_a, "spec-1",
            "2026-09-28T00:00:00Z", "2026-09-28T00:30:00Z",
        )
        # 上一周（09-21~09-27）不受影响，仍有完整 120 分钟
        self.svc.create_booking(
            self.eng_a, "line-1",
            "2026-09-25T01:00:00Z", "2026-09-25T03:00:00Z",
        )
        report_this = self.svc.quota_report(
            self.admin, "team-a", week_of="2026-09-28T12:00:00Z"
        )[0]
        self.assertEqual(report_this["booked_minutes"], 30)
        self.assertEqual(report_this["remaining_minutes"], 90)
        report_last = self.svc.quota_report(
            self.admin, "team-a", week_of="2026-09-25T12:00:00Z"
        )[0]
        self.assertEqual(report_last["booked_minutes"], 120)

    def test_cancel_releases_quota(self) -> None:
        booking = self.svc.create_booking(
            self.eng_a, "spec-1",
            "2026-09-28T00:00:00Z", "2026-09-28T02:00:00Z",
        )
        with self.assertRaises(QuotaExceeded):
            self.svc.create_booking(
                self.eng_a, "line-1",
                "2026-09-28T03:00:00Z", "2026-09-28T03:01:00Z",
            )
        self.svc.cancel_booking(self.eng_a, booking["booking_id"], "释放配额")
        again = self.svc.create_booking(
            self.eng_a, "line-1",
            "2026-09-28T03:00:00Z", "2026-09-28T03:01:00Z",
        )
        self.assertTrue(again["booking_id"])

    def test_engineer_sees_only_own_team_report(self) -> None:
        with self.assertRaises(PermissionError):
            self.svc.quota_report(self.eng_a, "team-b")
        report = self.svc.quota_report(self.eng_a)
        self.assertEqual([r["team_id"] for r in report], ["team-a"])


class ConcurrencyTests(ScheduleTestBase):
    def test_parallel_bookings_same_slot_only_one_succeeds(self) -> None:
        results: list = []
        errors: list = []

        def book(token: str) -> None:
            try:
                results.append(
                    self.svc.create_booking(
                        token, "spec-1",
                        "2026-09-28T01:00:00Z", "2026-09-28T02:00:00Z",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=book, args=(self.eng_a,)),
            threading.Thread(target=book, args=(self.eng_b,)),
            threading.Thread(target=book, args=(self.eng_a,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, f"只应有一个预约成功: {results}")
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(e, Conflict) for e in errors))
        stored = self.svc.list_bookings(self.admin, resource_id="spec-1")
        self.assertEqual(len(stored), 1)

    def test_separate_connections_still_serialize(self) -> None:
        """跨连接（模拟多进程/多实例）下，BEGIN IMMEDIATE + 事务内冲突检测仍唯一成功。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.sqlite3"
            bootstrap = ScheduleService(str(path))
            bootstrap.auth.create_user("admin1", "admin-pass-123", "admin")
            bootstrap.auth.create_user("eng-a", "eng-a-pass-123", "engineer")
            bootstrap.auth.create_user("eng-b", "eng-b-pass-123", "engineer")
            admin = bootstrap.auth.login("admin1", "admin-pass-123")
            bootstrap.create_team(admin, "team-a", "A 组", 600)
            bootstrap.create_team(admin, "team-b", "B 组", 600)
            bootstrap.create_resource(admin, "spec-1", "光谱仪 1", "spectrometer")
            bootstrap.add_member(admin, "eng-a", "team-a")
            bootstrap.add_member(admin, "eng-b", "team-b")
            bootstrap.db.close()

            svc_a = ScheduleService(str(path))
            svc_b = ScheduleService(str(path))
            token_a = svc_a.auth.login("eng-a", "eng-a-pass-123")
            token_b = svc_b.auth.login("eng-b", "eng-b-pass-123")
            outcomes: list[str] = []
            lock = threading.Lock()

            def book(svc: ScheduleService, token: str, label: str) -> None:
                try:
                    svc.create_booking(
                        token, "spec-1",
                        "2026-09-28T01:00:00Z", "2026-09-28T02:00:00Z",
                    )
                    with lock:
                        outcomes.append(f"{label}:ok")
                except Conflict:
                    with lock:
                        outcomes.append(f"{label}:conflict")

            t1 = threading.Thread(target=book, args=(svc_a, token_a, "a"))
            t2 = threading.Thread(target=book, args=(svc_b, token_b, "b"))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            ok = [o for o in outcomes if o.endswith(":ok")]
            self.assertEqual(len(ok), 1, outcomes)
            conflicts = [o for o in outcomes if o.endswith(":conflict")]
            self.assertEqual(len(conflicts), 1, outcomes)
            svc_a.db.close()
            svc_b.db.close()


if __name__ == "__main__":
    unittest.main()
