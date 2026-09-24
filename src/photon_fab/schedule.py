"""共享设备预约应用服务。

覆盖资源（光谱仪 / 封测线）、团队、团队周配额、预约、冲突检测、
配额统计和审计。时间一律以 UTC 存储和比较，区间采用半开语义
``[starts_at, ends_at)``：首尾相接不算冲突。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .auth import Auth
from .errors import Conflict, InvalidState, QuotaExceeded
from .storage_schedule import SCHEMA, transaction

Clock = Callable[[], datetime]

WEEK = timedelta(days=7)
# 单次预约最多跨 52 个周桶，防止配额计算被异常输入放大。
MAX_SPANNED_WEEKS = 52


def utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str) -> datetime:
    """解析 ISO-8601 时间并归一化为带时区的 UTC，拒绝朴素时间。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("time must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 time: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timezone is required; use UTC (e.g. 2026-09-24T08:00:00Z)")
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def week_start(value: datetime) -> datetime:
    """返回 ``value`` 所在周的周一 00:00 UTC。"""
    return (value - timedelta(days=value.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def iter_weeks(starts_at: datetime, ends_at: datetime) -> Iterable[tuple[datetime, datetime]]:
    cursor = week_start(starts_at)
    end_week = week_start(ends_at - timedelta(microseconds=1))
    count = 0
    while cursor <= end_week:
        count += 1
        if count > MAX_SPANNED_WEEKS:
            raise ValueError("booking spans too many weeks")
        yield cursor, cursor + WEEK
        cursor += WEEK


def overlap_minutes(
    starts_at: datetime,
    ends_at: datetime,
    window_start: datetime,
    window_end: datetime,
) -> int:
    """区间与窗口重叠的整分钟数（秒级精度，向上取整到分钟）。"""
    start = max(starts_at, window_start)
    end = min(ends_at, window_end)
    if end <= start:
        return 0
    seconds = (end - start).total_seconds()
    return int(-(-seconds // 60))  # ceil，保证用量计费不少于实际占用


class ScheduleService:
    def __init__(
        self,
        database: str = ":memory:",
        auth: Auth | None = None,
        clock: Clock = utc_clock,
    ) -> None:
        if auth is None:
            from .storage_schedule import connect

            self.db = connect(database)
            self.auth = Auth(self.db)
        else:
            self.auth = auth
            self.db = auth.db
            # 与既有 PhotonService 共享连接时幂等建表。
            self.db.executescript(SCHEMA)
            self.db.commit()
        self.clock = clock
        # 进程内串行化所有写事务；跨进程的唯一成功由 SQLite 写锁保证。
        self._write_lock = threading.RLock()

    # ------------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("clock must return timezone-aware UTC datetimes")
        return now.astimezone(timezone.utc)

    def _audit(
        self, entity_type: str, entity_id: str, action: str, actor_id: str, payload: dict
    ) -> None:
        self.db.execute(
            "INSERT INTO sched_audit_events(entity_type,entity_id,action,actor_id,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                action,
                actor_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                iso(self._now()),
            ),
        )

    def _member_team(self, user_id: str) -> str | None:
        row = self.db.execute(
            "SELECT team_id FROM sched_team_members WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _booking_dict(row) -> dict:
        data = dict(row)
        data["duration_minutes"] = int(
            (parse_utc(data["ends_at"]) - parse_utc(data["starts_at"])).total_seconds() // 60
        )
        return data

    def _get_booking(self, booking_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM sched_bookings WHERE booking_id=?", (booking_id,)
        ).fetchone()
        if not row:
            raise KeyError(booking_id)
        return dict(row)

    def _assert_no_overlap(
        self, resource_id: str, starts_at: datetime, ends_at: datetime, exclude_id: str | None
    ) -> None:
        query = (
            "SELECT booking_id,team_id,starts_at,ends_at FROM sched_bookings "
            "WHERE resource_id=? AND state='booked' AND starts_at < ? AND ends_at > ?"
        )
        params: list = [resource_id, iso(ends_at), iso(starts_at)]
        if exclude_id is not None:
            query += " AND booking_id <> ?"
            params.append(exclude_id)
        row = self.db.execute(query, params).fetchone()
        if row:
            raise Conflict(
                f"resource {resource_id} is already booked by {row['booking_id']} "
                f"({row['starts_at']} - {row['ends_at']})"
            )

    def _assert_within_quota(
        self,
        team_id: str,
        starts_at: datetime,
        ends_at: datetime,
        exclude_id: str | None,
    ) -> None:
        quota_row = self.db.execute(
            "SELECT weekly_quota_minutes FROM sched_teams WHERE team_id=?", (team_id,)
        ).fetchone()
        if not quota_row:
            raise KeyError(team_id)
        quota = int(quota_row[0])
        weeks = list(iter_weeks(starts_at, ends_at))
        first_start = weeks[0][0]
        last_end = weeks[-1][1]
        query = (
            "SELECT starts_at,ends_at FROM sched_bookings "
            "WHERE team_id=? AND state='booked' AND starts_at < ? AND ends_at > ?"
        )
        params: list = [team_id, iso(last_end), iso(first_start)]
        if exclude_id is not None:
            query += " AND booking_id <> ?"
            params.append(exclude_id)
        rows = self.db.execute(query, params).fetchall()
        for window_start, window_end in weeks:
            used = 0
            for row in rows:
                used += overlap_minutes(
                    parse_utc(row[0]), parse_utc(row[1]), window_start, window_end
                )
            used += overlap_minutes(starts_at, ends_at, window_start, window_end)
            if used > quota:
                raise QuotaExceeded(
                    f"team {team_id} weekly quota {quota} minutes exceeded for week "
                    f"{window_start.date().isoformat()}: would use {used}"
                )

    # ------------------------------------------------------------------ teams

    def create_team(self, token: str, team_id: str, name: str, weekly_quota_minutes: int = 0) -> dict:
        actor = self.auth.require(token, "admin")
        if not team_id.strip() or not name.strip():
            raise ValueError("team_id and name are required")
        quota = int(weekly_quota_minutes)
        if quota < 0:
            raise ValueError("weekly_quota_minutes must be >= 0")
        now = iso(self._now())
        with self._write_lock, transaction(self.db):
            if self.db.execute("SELECT 1 FROM sched_teams WHERE team_id=?", (team_id,)).fetchone():
                raise ValueError(f"team {team_id} already exists")
            self.db.execute(
                "INSERT INTO sched_teams(team_id,name,weekly_quota_minutes,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                (team_id, name, quota, now, now),
            )
            self._audit("team", team_id, "team.created", actor.user_id, {"name": name, "weekly_quota_minutes": quota})
        return self.get_team(token, team_id)

    def set_quota(self, token: str, team_id: str, weekly_quota_minutes: int) -> dict:
        """管理员设置团队每周配额（分钟）。"""
        actor = self.auth.require(token, "admin")
        quota = int(weekly_quota_minutes)
        if quota < 0:
            raise ValueError("weekly_quota_minutes must be >= 0")
        with self._write_lock, transaction(self.db):
            if not self.db.execute("SELECT 1 FROM sched_teams WHERE team_id=?", (team_id,)).fetchone():
                raise KeyError(team_id)
            old = self.db.execute(
                "SELECT weekly_quota_minutes FROM sched_teams WHERE team_id=?", (team_id,)
            ).fetchone()[0]
            now = iso(self._now())
            self.db.execute(
                "UPDATE sched_teams SET weekly_quota_minutes=?,updated_at=? WHERE team_id=?",
                (quota, now, team_id),
            )
            self._audit(
                "team", team_id, "quota.changed", actor.user_id,
                {"old_minutes": int(old), "new_minutes": quota},
            )
        return self.get_team(token, team_id)

    def add_member(self, token: str, user_id: str, team_id: str) -> dict:
        """管理员把工程师（或任何用户）编入团队；一个用户只属于一个团队。"""
        actor = self.auth.require(token, "admin")
        with self._write_lock, transaction(self.db):
            if not self.db.execute("SELECT 1 FROM sched_teams WHERE team_id=?", (team_id,)).fetchone():
                raise KeyError(team_id)
            if not self.db.execute("SELECT 1 FROM users WHERE user_id=? AND active=1", (user_id,)).fetchone():
                raise KeyError(user_id)
            now = iso(self._now())
            existing = self.db.execute(
                "SELECT team_id FROM sched_team_members WHERE user_id=?", (user_id,)
            ).fetchone()
            if existing:
                if existing[0] == team_id:
                    return {"user_id": user_id, "team_id": team_id}
                raise InvalidState(f"user {user_id} already belongs to team {existing[0]}")
            self.db.execute(
                "INSERT INTO sched_team_members(user_id,team_id,created_at) VALUES(?,?,?)",
                (user_id, team_id, now),
            )
            self._audit("team", team_id, "member.added", actor.user_id, {"user_id": user_id})
        return {"user_id": user_id, "team_id": team_id}

    def get_team(self, token: str, team_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM sched_teams WHERE team_id=?", (team_id,)).fetchone()
        if not row:
            raise KeyError(team_id)
        return dict(row)

    def list_teams(self, token: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM sched_teams ORDER BY team_id")]

    # ------------------------------------------------------------------ resources

    def create_resource(self, token: str, resource_id: str, name: str, kind: str) -> dict:
        actor = self.auth.require(token, "admin")
        if kind not in {"spectrometer", "packaging_line"}:
            raise ValueError("kind must be 'spectrometer' or 'packaging_line'")
        if not resource_id.strip() or not name.strip():
            raise ValueError("resource_id and name are required")
        now = iso(self._now())
        with self._write_lock, transaction(self.db):
            if self.db.execute("SELECT 1 FROM sched_resources WHERE resource_id=?", (resource_id,)).fetchone():
                raise ValueError(f"resource {resource_id} already exists")
            self.db.execute(
                "INSERT INTO sched_resources(resource_id,name,kind,active,created_by,created_at)"
                " VALUES(?,?,?,1,?,?)",
                (resource_id, name, kind, actor.user_id, now),
            )
            self._audit(
                "resource", resource_id, "resource.created", actor.user_id,
                {"name": name, "kind": kind},
            )
        return self.get_resource(token, resource_id)

    def get_resource(self, token: str, resource_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT * FROM sched_resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        if not row:
            raise KeyError(resource_id)
        return dict(row)

    def list_resources(self, token: str, include_inactive: bool = False) -> list[dict]:
        self.auth.require(token, "read")
        sql = "SELECT * FROM sched_resources"
        if not include_inactive:
            sql += " WHERE active=1"
        sql += " ORDER BY resource_id"
        return [dict(r) for r in self.db.execute(sql)]

    # ------------------------------------------------------------------ bookings

    def create_booking(
        self, token: str, resource_id: str, starts_at: str, ends_at: str, team_id: str | None = None
    ) -> dict:
        """为所属团队创建预约；并发下同一时段只有一个请求成功。"""
        actor = self.auth.require(token, "schedule")
        start = parse_utc(starts_at)
        end = parse_utc(ends_at)
        if end <= start:
            raise ValueError("ends_at must be after starts_at")
        is_admin = actor.role == "admin"
        member_team = self._member_team(actor.user_id)
        if not is_admin:
            if not member_team:
                raise PermissionError("user is not assigned to a team")
            if team_id is not None and team_id != member_team:
                raise PermissionError("can only book for your own team")
            team_id = member_team
        elif team_id is None:
            if not member_team:
                raise PermissionError("admin without a team must specify team_id")
        else:
            team_id = team_id
        with self._write_lock, transaction(self.db):
            if not self.db.execute(
                "SELECT 1 FROM sched_resources WHERE resource_id=? AND active=1", (resource_id,)
            ).fetchone():
                raise KeyError(resource_id)
            if not self.db.execute("SELECT 1 FROM sched_teams WHERE team_id=?", (team_id,)).fetchone():
                raise KeyError(team_id)
            self._assert_no_overlap(resource_id, start, end, None)
            self._assert_within_quota(team_id, start, end, None)
            booking_id = uuid.uuid4().hex
            now = iso(self._now())
            self.db.execute(
                "INSERT INTO sched_bookings(booking_id,resource_id,team_id,starts_at,ends_at,"
                "state,created_by,created_at,revision) VALUES(?,?,?,?,?,'booked',?,?,1)",
                (booking_id, resource_id, team_id, iso(start), iso(end), actor.user_id, now),
            )
            self._audit(
                "booking", booking_id, "booking.created", actor.user_id,
                {
                    "resource_id": resource_id,
                    "team_id": team_id,
                    "starts_at": iso(start),
                    "ends_at": iso(end),
                },
            )
        return self.get_booking(token, booking_id)

    def get_booking(self, token: str, booking_id: str) -> dict:
        self.auth.require(token, "read")
        return self._booking_dict(self._get_booking_row(booking_id))

    def _get_booking_row(self, booking_id: str):
        row = self.db.execute("SELECT * FROM sched_bookings WHERE booking_id=?", (booking_id,)).fetchone()
        if not row:
            raise KeyError(booking_id)
        return row

    def list_bookings(
        self,
        token: str,
        resource_id: str | None = None,
        team_id: str | None = None,
        starts_after: str | None = None,
        starts_before: str | None = None,
        include_cancelled: bool = False,
    ) -> list[dict]:
        self.auth.require(token, "read")
        clauses: list[str] = []
        params: list = []
        if not include_cancelled:
            clauses.append("state='booked'")
        if resource_id:
            clauses.append("resource_id=?")
            params.append(resource_id)
        if team_id:
            clauses.append("team_id=?")
            params.append(team_id)
        if starts_after:
            clauses.append("starts_at >= ?")
            params.append(iso(parse_utc(starts_after)))
        if starts_before:
            clauses.append("starts_at < ?")
            params.append(iso(parse_utc(starts_before)))
        sql = "SELECT * FROM sched_bookings"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY starts_at,booking_id"
        return [self._booking_dict(r) for r in self.db.execute(sql, params)]

    def _authorize_mutation(self, token: str, booking: dict) -> tuple[object, bool]:
        """返回 (actor, is_admin)。团队外成员拒绝；开始后仅管理员可改写。"""
        actor = self.auth.require(token, "schedule")
        is_admin = actor.role == "admin"
        member_team = self._member_team(actor.user_id)
        if not is_admin and member_team != booking["team_id"]:
            raise PermissionError("can only modify your own team's bookings")
        if not is_admin and self._now() >= parse_utc(booking["starts_at"]):
            raise InvalidState("booking already started; only an administrator can modify it")
        if booking["state"] != "booked":
            raise InvalidState(f"booking is {booking['state']}")
        return actor, is_admin

    def cancel_booking(self, token: str, booking_id: str, reason: str) -> dict:
        if not reason or not reason.strip():
            raise ValueError("cancel reason is required")
        with self._write_lock, transaction(self.db):
            booking = self._get_booking(booking_id)
            actor, _ = self._authorize_mutation(token, booking)
            now_dt = self._now()
            self.db.execute(
                "UPDATE sched_bookings SET state='cancelled',cancelled_by=?,cancelled_at=?,"
                "cancel_reason=?,revision=revision+1 WHERE booking_id=?",
                (actor.user_id, iso(now_dt), reason.strip(), booking_id),
            )
            self._audit(
                "booking", booking_id, "booking.cancelled", actor.user_id,
                {"reason": reason.strip(), "started": now_dt >= parse_utc(booking["starts_at"])},
            )
        return self.get_booking(token, booking_id)

    def change_booking(
        self, token: str, booking_id: str, starts_at: str, ends_at: str
    ) -> dict:
        """变更预约时段（重新做冲突检测与配额校验），旧值进入审计。"""
        start = parse_utc(starts_at)
        end = parse_utc(ends_at)
        if end <= start:
            raise ValueError("ends_at must be after starts_at")
        with self._write_lock, transaction(self.db):
            booking = self._get_booking(booking_id)
            actor, _ = self._authorize_mutation(token, booking)
            self._assert_no_overlap(booking["resource_id"], start, end, booking_id)
            self._assert_within_quota(booking["team_id"], start, end, booking_id)
            self.db.execute(
                "UPDATE sched_bookings SET starts_at=?,ends_at=?,revision=revision+1"
                " WHERE booking_id=?",
                (iso(start), iso(end), booking_id),
            )
            self._audit(
                "booking", booking_id, "booking.changed", actor.user_id,
                {
                    "old": {"starts_at": booking["starts_at"], "ends_at": booking["ends_at"]},
                    "new": {"starts_at": iso(start), "ends_at": iso(end)},
                    "revision_before": int(booking["revision"]),
                },
            )
        return self.get_booking(token, booking_id)

    # ------------------------------------------------------------------ stats

    def quota_report(
        self, token: str, team_id: str | None = None, week_of: str | None = None
    ) -> list[dict]:
        """返回团队在指定 UTC 周（默认本周）的配额用量统计。"""
        actor = self.auth.require(token, "read")
        is_admin = actor.role == "admin"
        own_team = self._member_team(actor.user_id)
        if team_id is None:
            if is_admin:
                team_ids = [
                    r[0] for r in self.db.execute("SELECT team_id FROM sched_teams ORDER BY team_id")
                ]
            else:
                if not own_team:
                    raise PermissionError("user is not assigned to a team")
                team_ids = [own_team]
        else:
            if not is_admin and team_id != own_team:
                raise PermissionError("can only view your own team's quota")
            team_ids = [team_id]
        anchor = parse_utc(week_of) if week_of else self._now()
        w_start = week_start(anchor)
        w_end = w_start + WEEK
        report: list[dict] = []
        for tid in team_ids:
            team = self.db.execute(
                "SELECT name, weekly_quota_minutes FROM sched_teams WHERE team_id=?", (tid,)
            ).fetchone()
            if not team:
                raise KeyError(tid)
            used = 0
            rows = self.db.execute(
                "SELECT starts_at,ends_at FROM sched_bookings "
                "WHERE team_id=? AND state='booked' AND starts_at < ? AND ends_at > ?",
                (tid, iso(w_end), iso(w_start)),
            ).fetchall()
            for row in rows:
                used += overlap_minutes(parse_utc(row[0]), parse_utc(row[1]), w_start, w_end)
            quota = int(team[1])
            report.append(
                {
                    "team_id": tid,
                    "team_name": team[0],
                    "week_start": iso(w_start),
                    "week_end": iso(w_end),
                    "quota_minutes": quota,
                    "booked_minutes": used,
                    "remaining_minutes": max(0, quota - used),
                    "over_quota": used > quota,
                }
            )
        return report

    # ------------------------------------------------------------------ audit

    def audit_events(
        self,
        token: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        self.auth.require(token, "read")
        clauses: list[str] = []
        params: list = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        sql = "SELECT * FROM sched_audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_id DESC LIMIT ?"
        params.append(int(limit))
        events: list[dict] = []
        for row in self.db.execute(sql, params):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        return events
