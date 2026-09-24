"""共享设备预约域的 SQLite 模式与事务辅助。

所有时间戳均以 UTC ISO-8601 字符串保存，由服务层统一转换。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sched_teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    weekly_quota_minutes INTEGER NOT NULL DEFAULT 0
        CHECK(weekly_quota_minutes >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sched_team_members (
    user_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES sched_teams(team_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sched_resources (
    resource_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('spectrometer','packaging_line')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sched_bookings (
    booking_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES sched_resources(resource_id),
    team_id TEXT NOT NULL REFERENCES sched_teams(team_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'booked'
        CHECK(state IN ('booked','cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    cancelled_by TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    CHECK(ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_bookings_resource_time
ON sched_bookings(resource_id, state, starts_at, ends_at);

CREATE INDEX IF NOT EXISTS idx_bookings_team_time
ON sched_bookings(team_id, state, starts_at);

CREATE TABLE IF NOT EXISTS sched_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sched_audit_entity
ON sched_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：写操作由服务层的 RLock 串行化，
    # 允许 HTTP 工作线程共享同一连接。
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    # IMMEDIATE 在第一时间取得写锁，保证并发预约的检查-写入串行化：
    # 同一资源同一时段的两个请求只会有一个成功提交。
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
