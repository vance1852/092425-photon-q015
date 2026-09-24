"""设备预约域的离线冒烟验收。

在内存数据库中走完“建档 → 配额 → 预约 → 冲突 → 改期 → 开始后锁定 →
管理员强制取消 → 审计链校验”的完整流程，输出一行 JSON。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .clock import FrozenClock
from .errors import Conflict, Forbidden
from .service import BookingService
from .storage import connect


def run() -> dict:
    clock = FrozenClock(datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc))
    service = BookingService(connect(":memory:"), clock)
    service.bootstrap_admin("admin", "team-ops")

    service.create_team("admin", "team-a", "光电器件组", weekly_quota_minutes=180)
    service.create_team("admin", "team-b", "先进封测组", weekly_quota_minutes=240)
    service.create_resource("admin", "spectrometer-1", "共享光谱仪 A", "spectrometer")
    service.create_resource("admin", "packaging-line-1", "共享封测线 1", "packaging")
    service.create_user("admin", "alice", "Alice", "team-a", "engineer")
    service.create_user("admin", "bob", "Bob", "team-b", "engineer")

    first = service.create_reservation(
        "alice", "spectrometer-1", "2026-09-21T10:00:00Z", "2026-09-21T11:30:00Z",
        "CMOS 图像传感器光谱扫描",
    )

    conflict_seen = False
    try:
        service.create_reservation(
            "bob", "spectrometer-1", "2026-09-21T11:00:00Z", "2026-09-21T12:00:00Z",
            "封测前抽检",
        )
    except Conflict:
        conflict_seen = True

    # 首尾相接（11:30 开始）不算冲突，可以预约。
    adjacent = service.create_reservation(
        "bob", "spectrometer-1", "2026-09-21T11:30:00Z", "2026-09-21T12:30:00Z",
        "封测前抽检",
    )

    # 再订 120 分钟会超过团队 A 的 180 分钟周配额（已有 90 分钟）。
    quota_blocked = False
    try:
        service.create_reservation(
            "alice", "spectrometer-1", "2026-09-21T14:00:00Z", "2026-09-21T16:00:00Z",
            "复测",
        )
    except Conflict:
        quota_blocked = True
    alice_recheck = service.create_reservation(
        "alice", "spectrometer-1", "2026-09-21T14:00:00Z", "2026-09-21T14:30:00Z",
        "复测",
    )

    packaging = service.create_reservation(
        "bob", "packaging-line-1", "2026-09-22T09:00:00Z", "2026-09-22T10:30:00Z",
        "晶圆级封装", idempotency_key="bob-pkg-1",
    )
    replayed = service.create_reservation(
        "bob", "packaging-line-1", "2026-09-22T09:00:00Z", "2026-09-22T10:30:00Z",
        "晶圆级封装", idempotency_key="bob-pkg-1",
    )
    assert replayed["reservation_id"] == packaging["reservation_id"]

    service.reschedule_reservation(
        "bob", packaging["reservation_id"],
        "2026-09-22T10:30:00Z", "2026-09-22T11:30:00Z",
    )

    # 时钟推进到 10:00，10:00 开始的预约已经开始：普通用户不可改写。
    usage_before_cancel = service.weekly_quota_usage("alice", "team-a", "2026-W39")
    clock.advance(hours=2)
    engineer_cancel_blocked = False
    try:
        service.cancel_reservation("alice", first["reservation_id"], "临时取消")
    except Forbidden:
        engineer_cancel_blocked = True
    # 跨团队同样被拒绝（14:00 的预约尚未开始，但不属于 Bob 的团队）。
    cross_blocked = False
    try:
        service.cancel_reservation("bob", alice_recheck["reservation_id"], "替别人取消")
    except Forbidden:
        cross_blocked = True
    admin_cancelled = service.cancel_reservation("admin", first["reservation_id"], "设备检修，管理员强制取消")
    usage_after_cancel = service.weekly_quota_usage("alice", "team-a", "2026-W39")

    usage_a = usage_after_cancel
    chain = service.verify_chain("admin")
    events = service.audit_events("admin")

    return {
        "status": "ok",
        "overlap_conflict_detected": conflict_seen,
        "adjacent_allowed": adjacent["status"] == "booked",
        "quota_blocked": quota_blocked,
        "idempotent_replay": replayed["reservation_id"] == packaging["reservation_id"],
        "started_reservation_locked_for_engineer": engineer_cancel_blocked and cross_blocked,
        "admin_force_cancel": admin_cancelled["status"] == "cancelled",
        "team_a_booked_minutes": usage_a["booked_minutes"],
        "team_a_remaining_minutes": usage_a["remaining_minutes"],
        "quota_released_on_cancel": (
            usage_before_cancel["booked_minutes"] == 120
            and usage_after_cancel["booked_minutes"] == 30
        ),
        "audit_events": len(events),
        "audit_chain_valid": chain["valid"],
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
