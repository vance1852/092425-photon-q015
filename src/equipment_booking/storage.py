"""设备预约域的 SQLite 模式与事务辅助。

所有写入都在 ``BEGIN IMMEDIATE`` 事务中进行：SQLite 会在事务开始时立即
获取保留锁，因此并发的两个预约请求会被串行化，先到者提交，后到者在同一
事务中重新读取并检测冲突，保证“同一设备同一时段只有一个预约成功”。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    weekly_quota_minutes INTEGER NOT NULL DEFAULT 0
        CHECK(weekly_quota_minutes >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    role TEXT NOT NULL CHECK(role IN ('engineer','admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked'
        CHECK(status IN ('booked','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);

-- 活跃预约在“设备×起始时刻”上的数据库级唯一约束：重复提交或共享上界
-- 的区间会被直接拒绝。其余重叠情形由事务内的区间谓词拦截。
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_active_slot
ON reservations(resource_id, starts_at) WHERE status = 'booked';

CREATE INDEX IF NOT EXISTS idx_reservations_lookup
ON reservations(resource_id, status, starts_at, ends_at);

CREATE INDEX IF NOT EXISTS idx_reservations_team_week
ON reservations(team_id, status, starts_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_idempotency
ON reservations(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS reservation_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservation_audit_entity
ON reservation_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：ThreadingHTTPServer 会把请求分发到工作线程，
    # 而所有写事务均以 BEGIN IMMEDIATE 串行化，SQLite 文件/WAL 锁负责
    # 跨线程互斥，因此共享连接是安全的。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启立即写事务；调用方可在其中完成“读取-判断-写入”的原子序列。"""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
