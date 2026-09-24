"""设备预约的应用服务：资源、团队、配额、预约与哈希审计。

身份通过 ``X-Actor-Id`` 传递，角色只有 ``engineer`` 与 ``admin``：

* 工程师只能为自己所属的团队创建、取消或变更预约；
* 管理员可以管理团队配额、资源，并可跨团队操作；
* 预约开始之后，普通用户（工程师）不能再取消或变更，管理员的强制
  操作同样写入审计。

所有时间在入口处解析并归一化为 UTC，内部统一存成 ``...Z`` 文本，
字典序即时间顺序，因此可以直接在 SQL 中做区间比较。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, parse_week, utc_text, week_start
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .planning import canonical_json, intersection_minutes, spanned_weeks
from .storage import initialize, transaction

ROLES = {"engineer", "admin"}
GENESIS_HASH = "0" * 64


class BookingService:
    def __init__(self, connection: sqlite3.Connection, clock: Any | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now_text(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM booking_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _team_row(self, team_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
        if row is None:
            raise NotFound("团队不存在")
        return row

    def _resource_row(self, resource_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("设备不存在")
        return row

    def _reservation_row(self, reservation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        return row

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM reservation_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = GENESIS_HASH if previous is None else previous["event_hash"]
        created_at = self._now_text()
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": created_at,
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO reservation_audit_events(entity_type,entity_id,event_type,"
            "actor_id,payload_json,previous_hash,event_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                created_at,
            ),
        )

    # ----------------------------------------------------------------- 团队

    def create_team(self, actor_id: str, team_id: str, name: str, weekly_quota_minutes: int = 0) -> dict[str, Any]:
        self._require_admin(actor_id)
        if not team_id.strip() or not name.strip():
            raise ValidationFailed("团队编号和名称不能为空")
        quota = int(weekly_quota_minutes)
        if quota < 0:
            raise ValidationFailed("每周配额不能为负")
        with transaction(self.connection):
            if self.connection.execute("SELECT 1 FROM teams WHERE team_id=?", (team_id,)).fetchone():
                raise Conflict("团队已存在")
            self.connection.execute(
                "INSERT INTO teams(team_id,name,weekly_quota_minutes,created_at) VALUES(?,?,?,?)",
                (team_id, name, quota, self._now_text()),
            )
            self._audit("team", team_id, "team.created", actor_id, {"name": name, "weekly_quota_minutes": quota})
        return dict(self._team_row(team_id))

    def set_weekly_quota(self, actor_id: str, team_id: str, weekly_quota_minutes: int) -> dict[str, Any]:
        """管理员设置团队每周配额；0 表示不限制。"""
        self._require_admin(actor_id)
        quota = int(weekly_quota_minutes)
        if quota < 0:
            raise ValidationFailed("每周配额不能为负")
        with transaction(self.connection):
            team = self._team_row(team_id)
            old_quota = team["weekly_quota_minutes"]
            if old_quota == quota:
                return dict(team)
            self.connection.execute(
                "UPDATE teams SET weekly_quota_minutes=? WHERE team_id=?", (quota, team_id)
            )
            self._audit(
                "team", team_id, "team.quota_changed", actor_id,
                {"old_weekly_quota_minutes": old_quota, "new_weekly_quota_minutes": quota},
            )
        return dict(self._team_row(team_id))

    def team(self, actor_id: str, team_id: str) -> dict[str, Any]:
        self._user(actor_id)
        return dict(self._team_row(team_id))

    def teams(self, actor_id: str) -> list[dict[str, Any]]:
        self._user(actor_id)
        return [dict(r) for r in self.connection.execute("SELECT * FROM teams ORDER BY team_id")]

    # ----------------------------------------------------------------- 用户

    def has_users(self) -> bool:
        return self.connection.execute("SELECT count(*) FROM booking_users").fetchone()[0] > 0

    def create_user(
        self, actor_id: str, user_id: str, display_name: str, team_id: str, role: str = "engineer"
    ) -> dict[str, Any]:
        """管理员建档；首个管理员可在无用户时通过 bootstrap_admin 直接登记。"""
        if role not in ROLES:
            raise ValidationFailed(f"角色必须是 {sorted(ROLES)} 之一")
        with transaction(self.connection):
            existing = self.connection.execute(
                "SELECT 1 FROM booking_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if existing:
                raise Conflict("用户已存在")
            # 没有任何用户时（初始化）允许自助建档，之后必须由管理员操作。
            is_bootstrap = self.connection.execute("SELECT count(*) FROM booking_users").fetchone()[0] == 0
            if not is_bootstrap:
                self._require_admin(actor_id)
            self._team_row(team_id)
            self.connection.execute(
                "INSERT INTO booking_users(user_id,display_name,team_id,role,active,created_at) "
                "VALUES(?,?,?,?,1,?)",
                (user_id, display_name, team_id, role, self._now_text()),
            )
            self._audit(
                "user", user_id, "user.created", actor_id or user_id,
                {"display_name": display_name, "team_id": team_id, "role": role},
            )
        return self.user(actor_id or user_id, user_id)

    def deactivate_user(self, actor_id: str, user_id: str) -> dict[str, Any]:
        self._require_admin(actor_id)
        with transaction(self.connection):
            self._user(user_id)
            self.connection.execute("UPDATE booking_users SET active=0 WHERE user_id=?", (user_id,))
            self._audit("user", user_id, "user.deactivated", actor_id, {})
        return dict(self.connection.execute("SELECT * FROM booking_users WHERE user_id=?", (user_id,)).fetchone())

    def user(self, actor_id: str, user_id: str) -> dict[str, Any]:
        self._user(actor_id)
        return dict(self._user(user_id))

    def bootstrap_admin(self, admin_id: str = "admin", team_id: str = "team-ops") -> dict[str, Any]:
        """离线/容器入口：创建运维团队和首个管理员，已存在则直接返回。"""
        with transaction(self.connection):
            if not self.connection.execute("SELECT 1 FROM teams WHERE team_id=?", (team_id,)).fetchone():
                self.connection.execute(
                    "INSERT INTO teams(team_id,name,weekly_quota_minutes,created_at) VALUES(?,?,0,?)",
                    (team_id, "运维管理团队", self._now_text()),
                )
            if self.connection.execute("SELECT 1 FROM booking_users WHERE user_id=?", (admin_id,)).fetchone():
                return dict(self._user(admin_id))
        return self.create_user(admin_id, admin_id, "管理员", team_id, "admin")

    # ---------------------------------------------------------------- 资源

    def create_resource(self, actor_id: str, resource_id: str, name: str, kind: str) -> dict[str, Any]:
        self._require_admin(actor_id)
        if not resource_id.strip() or not name.strip() or not kind.strip():
            raise ValidationFailed("设备编号、名称和类型不能为空")
        with transaction(self.connection):
            if self.connection.execute("SELECT 1 FROM resources WHERE resource_id=?", (resource_id,)).fetchone():
                raise Conflict("设备已存在")
            self.connection.execute(
                "INSERT INTO resources(resource_id,name,kind,active,created_at) VALUES(?,?,?,1,?)",
                (resource_id, name, kind, self._now_text()),
            )
            self._audit(
                "resource", resource_id, "resource.created", actor_id,
                {"name": name, "kind": kind},
            )
        return dict(self._resource_row(resource_id))

    def deactivate_resource(self, actor_id: str, resource_id: str, reason: str) -> dict[str, Any]:
        self._require_admin(actor_id)
        if not reason.strip():
            raise ValidationFailed("停用原因不能为空")
        with transaction(self.connection):
            resource = self._resource_row(resource_id)
            if resource["active"]:
                self.connection.execute("UPDATE resources SET active=0 WHERE resource_id=?", (resource_id,))
                self._audit(
                    "resource", resource_id, "resource.deactivated", actor_id, {"reason": reason}
                )
        return dict(self._resource_row(resource_id))

    def resource(self, actor_id: str, resource_id: str) -> dict[str, Any]:
        self._user(actor_id)
        return dict(self._resource_row(resource_id))

    def resources(self, actor_id: str) -> list[dict[str, Any]]:
        self._user(actor_id)
        return [dict(r) for r in self.connection.execute("SELECT * FROM resources ORDER BY resource_id")]

    def schedule(
        self,
        actor_id: str,
        resource_id: str,
        window_start: str | None = None,
        window_end: str | None = None,
        include_cancelled: bool = False,
    ) -> dict[str, Any]:
        self._user(actor_id)
        self._resource_row(resource_id)
        if window_start is None:
            start = week_start(self.clock.now())
            end = start + timedelta(days=7)
            start_text, end_text = utc_text(start), utc_text(end)
        else:
            start = parse_utc(window_start)
            end = parse_utc(window_end) if window_end else start + timedelta(days=7)
            if end <= start:
                raise ValidationFailed("查询窗口结束时间必须晚于开始时间")
            start_text, end_text = utc_text(start), utc_text(end)
        sql = (
            "SELECT * FROM reservations WHERE resource_id=? AND starts_at < ? AND ends_at > ? "
            "ORDER BY starts_at, reservation_id"
        )
        if not include_cancelled:
            sql = (
                "SELECT * FROM reservations WHERE resource_id=? AND status='booked' "
                "AND starts_at < ? AND ends_at > ? ORDER BY starts_at, reservation_id"
            )
        rows = self.connection.execute(sql, (resource_id, end_text, start_text)).fetchall()
        return {
            "resource_id": resource_id,
            "window_start": start_text,
            "window_end": end_text,
            "reservations": [dict(r) for r in rows],
        }

    # ---------------------------------------------------------------- 预约

    @staticmethod
    def _parse_window(starts_at: str, ends_at: str) -> tuple[str, str]:
        start = parse_utc(starts_at)
        end = parse_utc(ends_at)
        if end <= start:
            raise ValidationFailed("预约结束时间必须晚于开始时间")
        return utc_text(start), utc_text(end)

    def _overlapping(
        self, resource_id: str, start_text: str, end_text: str, exclude_id: str | None = None
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM reservations WHERE resource_id=? AND status='booked' "
            "AND starts_at < ? AND ends_at > ?"
        )
        params: list[Any] = [resource_id, end_text, start_text]
        if exclude_id is not None:
            sql += " AND reservation_id <> ?"
            params.append(exclude_id)
        sql += " ORDER BY starts_at"
        return self.connection.execute(sql, params).fetchall()

    def _check_quota(
        self, team_id: str, start: datetime, end: datetime, exclude_id: str | None = None
    ) -> None:
        """对预约覆盖的每个 UTC 周分别做配额校验。配额为 0 表示不限制。"""
        team = self._team_row(team_id)
        quota = team["weekly_quota_minutes"]
        if quota <= 0:
            return
        weeks = spanned_weeks(start, end)
        # 取覆盖周到的最早周一开始、最晚周日结束作为候选区间，再按周截断。
        span_start, span_end = weeks[0][0], weeks[-1][1]
        span_start_text, span_end_text = utc_text(span_start), utc_text(span_end)
        sql = (
            "SELECT starts_at,ends_at FROM reservations WHERE team_id=? AND status='booked' "
            "AND starts_at < ? AND ends_at > ?"
        )
        params: list[Any] = [team_id, span_end_text, span_start_text]
        if exclude_id is not None:
            sql += " AND reservation_id <> ?"
            params.append(exclude_id)
        rows = self.connection.execute(sql, params).fetchall()
        for window_start, window_end in weeks:
            used = intersection_minutes(start, end, window_start, window_end)
            for row in rows:
                used += intersection_minutes(
                    parse_utc(row["starts_at"]), parse_utc(row["ends_at"]),
                    window_start, window_end,
                )
            if used > quota:
                raise Conflict(
                    f"团队 {team_id} 在 {utc_text(window_start)[:10]} 所在周的预约将达到 "
                    f"{used} 分钟，超过每周配额 {quota} 分钟"
                )

    def create_reservation(
        self,
        actor_id: str,
        resource_id: str,
        starts_at: str,
        ends_at: str,
        purpose: str,
        team_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        actor = self._user(actor_id)
        if not purpose or not purpose.strip():
            raise ValidationFailed("预约用途不能为空")
        start_text, end_text = self._parse_window(starts_at, ends_at)
        start, end = parse_utc(start_text), parse_utc(end_text)
        if start <= self.clock.now():
            raise ValidationFailed("预约开始时间必须晚于当前 UTC 时间")

        target_team = actor["team_id"]
        if team_id:
            if actor["role"] != "admin" and team_id != actor["team_id"]:
                raise Forbidden("工程师只能为所属团队创建预约")
            target_team = team_id
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValidationFailed("幂等键不能为空")

        with transaction(self.connection):
            # 幂等重放：相同键且相同请求体直接返回已保存结果，不重复占用；
            # 同键不同载荷视为冲突，避免借幂等键改写语义。
            if idempotency_key:
                replay = self.connection.execute(
                    "SELECT * FROM reservations WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if replay is not None:
                    same = (
                        replay["resource_id"] == resource_id
                        and replay["team_id"] == target_team
                        and replay["starts_at"] == start_text
                        and replay["ends_at"] == end_text
                        and replay["purpose"] == purpose.strip()
                    )
                    if not same:
                        raise Conflict("幂等键已被另一请求使用")
                    return dict(replay)
            resource = self._resource_row(resource_id)
            if not resource["active"]:
                raise InvalidState("设备已停用，不能新建预约")
            self._team_row(target_team)
            clashes = self._overlapping(resource_id, start_text, end_text)
            if clashes:
                raise Conflict(
                    f"设备 {resource_id} 在 {start_text} ~ {end_text} 与预约 "
                    f"{clashes[0]['reservation_id']} 时间重叠"
                )
            self._check_quota(target_team, start, end)
            reservation_id = f"rsv-{self.connection.execute('SELECT lower(hex(randomblob(8)))').fetchone()[0]}"
            now_text = self._now_text()
            try:
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,resource_id,team_id,starts_at,ends_at,"
                    "purpose,status,revision,idempotency_key,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,'booked',1,?,?,?,?)",
                    (reservation_id, resource_id, target_team, start_text, end_text,
                     purpose.strip(), idempotency_key, actor_id, now_text, now_text),
                )
            except sqlite3.IntegrityError as exc:
                # 并发下另一事务可能已提交同一时段：数据库级唯一约束兜底。
                raise Conflict("预约时段与他人同时提交的预约冲突") from exc
            self._audit(
                "reservation", reservation_id, "reservation.created", actor_id,
                {
                    "resource_id": resource_id,
                    "team_id": target_team,
                    "starts_at": start_text,
                    "ends_at": end_text,
                    "purpose": purpose.strip(),
                    "idempotency_key": idempotency_key,
                },
            )
        return dict(self._reservation_row(reservation_id))

    def cancel_reservation(self, actor_id: str, reservation_id: str, reason: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        if not reason or not reason.strip():
            raise ValidationFailed("取消原因不能为空")
        with transaction(self.connection):
            reservation = self._reservation_row(reservation_id)
            if actor["role"] != "admin" and reservation["team_id"] != actor["team_id"]:
                raise Forbidden("工程师只能取消所属团队的预约")
            if reservation["status"] != "booked":
                raise InvalidState("预约已取消，不能重复取消")
            started = parse_utc(reservation["starts_at"]) <= self.clock.now()
            if started and actor["role"] != "admin":
                raise Forbidden("预约开始后普通用户不能取消或变更，请联系管理员")
            self.connection.execute(
                "UPDATE reservations SET status='cancelled', revision=revision+1, updated_at=? "
                "WHERE reservation_id=?",
                (self._now_text(), reservation_id),
            )
            self._audit(
                "reservation", reservation_id, "reservation.cancelled", actor_id,
                {"reason": reason.strip(), "forced_by_admin": started and actor["role"] == "admin"},
            )
        return dict(self._reservation_row(reservation_id))

    def reschedule_reservation(
        self,
        actor_id: str,
        reservation_id: str,
        starts_at: str,
        ends_at: str,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """变更预约时间或用途；变更前后内容都写入审计。"""
        actor = self._user(actor_id)
        start_text, end_text = self._parse_window(starts_at, ends_at)
        start, end = parse_utc(start_text), parse_utc(end_text)
        with transaction(self.connection):
            reservation = self._reservation_row(reservation_id)
            if actor["role"] != "admin" and reservation["team_id"] != actor["team_id"]:
                raise Forbidden("工程师只能变更所属团队的预约")
            if reservation["status"] != "booked":
                raise InvalidState("已取消的预约不能变更，请重新创建")
            old_start = parse_utc(reservation["starts_at"])
            now = self.clock.now()
            if old_start <= now and actor["role"] != "admin":
                raise Forbidden("预约开始后普通用户不能取消或变更，请联系管理员")
            if start <= now:
                raise ValidationFailed("变更后的开始时间必须晚于当前 UTC 时间")
            new_purpose = purpose.strip() if purpose and purpose.strip() else reservation["purpose"]
            if purpose is not None and not purpose.strip():
                raise ValidationFailed("预约用途不能为空")
            clashes = self._overlapping(reservation["resource_id"], start_text, end_text, reservation_id)
            if clashes:
                raise Conflict(
                    f"设备 {reservation['resource_id']} 在 {start_text} ~ {end_text} 与预约 "
                    f"{clashes[0]['reservation_id']} 时间重叠"
                )
            self._check_quota(reservation["team_id"], start, end, reservation_id)
            self.connection.execute(
                "UPDATE reservations SET starts_at=?, ends_at=?, purpose=?, revision=revision+1, "
                "updated_at=? WHERE reservation_id=?",
                (start_text, end_text, new_purpose, self._now_text(), reservation_id),
            )
            self._audit(
                "reservation", reservation_id, "reservation.rescheduled", actor_id,
                {
                    "before": {
                        "starts_at": reservation["starts_at"],
                        "ends_at": reservation["ends_at"],
                        "purpose": reservation["purpose"],
                    },
                    "after": {
                        "starts_at": start_text,
                        "ends_at": end_text,
                        "purpose": new_purpose,
                    },
                },
            )
        return dict(self._reservation_row(reservation_id))

    def reservation(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        row = self._reservation_row(reservation_id)
        if actor["role"] != "admin" and row["team_id"] != actor["team_id"]:
            raise Forbidden("只能查看所属团队的预约")
        return dict(row)

    # ------------------------------------------------------------- 配额统计

    def weekly_quota_usage(self, actor_id: str, team_id: str, week: str | None = None) -> dict[str, Any]:
        """返回团队某个 UTC ISO 周的已用分钟、配额与剩余额度。"""
        actor = self._user(actor_id)
        if actor["role"] != "admin" and team_id != actor["team_id"]:
            raise Forbidden("工程师只能查看所属团队的配额")
        team = self._team_row(team_id)
        window_start = parse_week(week) if week else week_start(self.clock.now())
        window_end = window_start + timedelta(days=7)
        start_text, end_text = utc_text(window_start), utc_text(window_end)
        rows = self.connection.execute(
            "SELECT reservation_id,starts_at,ends_at FROM reservations "
            "WHERE team_id=? AND status='booked' AND starts_at < ? AND ends_at > ?",
            (team_id, end_text, start_text),
        ).fetchall()
        used = 0
        items: list[dict[str, Any]] = []
        for row in rows:
            minutes = intersection_minutes(
                parse_utc(row["starts_at"]), parse_utc(row["ends_at"]), window_start, window_end
            )
            used += minutes
            items.append({"reservation_id": row["reservation_id"], "charged_minutes": minutes})
        quota = team["weekly_quota_minutes"]
        iso = window_start.isocalendar()
        return {
            "team_id": team_id,
            "week": f"{iso.year}-W{iso.week:02d}",
            "week_start": start_text,
            "week_end": end_text,
            "weekly_quota_minutes": quota,
            "booked_minutes": used,
            "remaining_minutes": None if quota <= 0 else max(0, quota - used),
            "unlimited": quota <= 0,
            "reservation_count": len(items),
            "reservations": items,
        }

    # ----------------------------------------------------------------- 审计

    def audit_events(
        self, actor_id: str, entity_type: str | None = None, entity_id: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_admin(actor_id)
        sql = "SELECT * FROM reservation_audit_events"
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_id"
        return [dict(r) for r in self.connection.execute(sql, params).fetchall()]

    def verify_chain(self, actor_id: str) -> dict[str, Any]:
        """离线重算全部审计事件哈希，验证事件顺序与内容未被篡改。"""
        self._require_admin(actor_id)
        rows = self.connection.execute(
            "SELECT * FROM reservation_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = GENESIS_HASH
        valid = True
        for row in rows:
            if row["previous_hash"] != previous_hash:
                valid = False
                break
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": previous_hash,
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if calculated != row["event_hash"]:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    # ------------------------------------------------------------- 权限辅助

    def _require_admin(self, actor_id: str) -> sqlite3.Row:
        user = self._user(actor_id)
        if user["role"] != "admin":
            raise Forbidden("需要管理员权限")
        return user
