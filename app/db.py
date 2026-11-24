"""SQLite 连接与建表。

- 每个请求使用独立连接（FastAPI 依赖注入）。
- 开启 WAL：核算（只读快照）与登记（写事务）可并发。
- 写事务统一使用 BEGIN IMMEDIATE，立即获取写锁，保证并发登记串行化；
  校验失败即 ROLLBACK，原记录保留。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

DB_PATH = os.environ.get("APP_DB_PATH", "/data/app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS period (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS old_district (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id     INTEGER NOT NULL REFERENCES period(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    completed     INTEGER NOT NULL CHECK (completed >= 0),
    UNIQUE (period_id, code)
);

CREATE TABLE IF NOT EXISTS new_district (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id     INTEGER NOT NULL REFERENCES period(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    fee           INTEGER NOT NULL CHECK (fee > 0),
    UNIQUE (period_id, code)
);

CREATE TABLE IF NOT EXISTS overlap (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id         INTEGER NOT NULL REFERENCES period(id) ON DELETE CASCADE,
    new_district_id   INTEGER NOT NULL REFERENCES new_district(id) ON DELETE CASCADE,
    old_district_id   INTEGER NOT NULL REFERENCES old_district(id) ON DELETE CASCADE,
    building_total    INTEGER NOT NULL CHECK (building_total > 0),
    UNIQUE (period_id, new_district_id, old_district_id)
);

CREATE TABLE IF NOT EXISTS retrieval (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id         INTEGER NOT NULL REFERENCES period(id) ON DELETE CASCADE,
    new_district_id   INTEGER NOT NULL REFERENCES new_district(id) ON DELETE CASCADE,
    old_district_id   INTEGER NOT NULL REFERENCES old_district(id) ON DELETE CASCADE,
    completed_count   INTEGER NOT NULL CHECK (completed_count >= 0),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (period_id, new_district_id, old_district_id)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 同步依赖的进入/退出可能落在 anyio
    # 线程池的不同工作线程上；每个请求持有独立连接且事务显式管理，不会共享。
    conn = sqlite3.connect(
        db_path or DB_PATH, timeout=5.0, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：请求级连接，事务由具体服务函数控制。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 立即拿写锁；异常回滚，保留原记录。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def snapshot_read(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务：整次核算看到同一已提交状态，且不阻塞登记。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()
