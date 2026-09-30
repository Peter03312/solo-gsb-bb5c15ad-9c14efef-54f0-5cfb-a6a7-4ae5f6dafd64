"""SQLite 连接管理、仅追加（append-only）表结构与初始化。

版本与历史模型
==============
* ``versions``          —— 全局单调版本，每次成功变更追加一行，版本号永不复用。
* ``requests``          —— 请求标识（idempotency）流水，记录动作、内容摘要与结果，
                           相同标识 + 相同内容可重放，标识复用但内容不同则冲突。
  ``certificate_versions`` —— 证书状态行。每个证书编号在一个版本中最多有一行“生效中”
                           的行，行区间 ``[version_valid_from, version_valid_to)``
                           左闭右开；纠正/撤销不修改历史行，只把旧行置为失效并追加新行。
* ``readings``          —— 读数一旦写入永不修改（仅追加），测量时间不可变。

读数结论不物化：结论 = f(读数, 任意已发布版本)。版本 v 时，先取该版本生效中的
证书行（``version_valid_from <= v < version_valid_to``），再按测量时间是否落入证书的
UTC 左闭右开有效期匹配。这样撤销/纠正天然改变当前结论，而旧版本的结论及其所依证书
仍可原样查询。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version        INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL,
    request_id     TEXT NOT NULL UNIQUE,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    request_id     TEXT PRIMARY KEY,
    action         TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    new_version    INTEGER NOT NULL,
    response_json  TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (new_version) REFERENCES versions(version)
);

CREATE TABLE IF NOT EXISTS certificate_versions (
    cert_no            TEXT NOT NULL,
    device_id          TEXT NOT NULL,
    valid_from_utc     TEXT NOT NULL,
    valid_to_utc       TEXT NOT NULL,
    value_min          REAL NOT NULL,
    value_max          REAL NOT NULL,
    version_valid_from INTEGER NOT NULL,
    version_valid_to   INTEGER,
    state              TEXT NOT NULL CHECK (state IN ('ACTIVE','SUPERSEDED','REVOKED')),
    superseded_by_cert TEXT,
    PRIMARY KEY (cert_no, version_valid_from),
    FOREIGN KEY (version_valid_from) REFERENCES versions(version)
);

CREATE TABLE IF NOT EXISTS readings (
    reading_no       TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL,
    measured_at_utc  TEXT NOT NULL,
    value            REAL NOT NULL,
    submitted_version INTEGER NOT NULL,
    created_at_utc   TEXT NOT NULL,
    FOREIGN KEY (submitted_version) REFERENCES versions(version)
);

CREATE INDEX IF NOT EXISTS idx_cert_device_state
    ON certificate_versions(device_id, version_valid_from, version_valid_to);
CREATE INDEX IF NOT EXISTS idx_reading_device_time
    ON readings(device_id, measured_at_utc);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开一个带外键约束、Row 工厂的连接（每次请求独立连接）。

    FastAPI 同步端点在线程池中执行（依赖注入创建连接与端点使用连接可能在
    不同线程），而连接仅在单个请求内使用、不跨请求共享，因此关闭同线程校验。
    """
    conn = sqlite3.connect(
        path or db_path(), timeout=30, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 让 Compose 健康检查与读写并发更顺畅。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str | None = None) -> None:
    """创建表与索引（幂等）；同时供启动时与 ``python -m app.db init`` 使用。"""
    target = path or db_path()
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def latest_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM versions").fetchone()
    return int(row[0])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="初始化或检查追溯服务数据库")
    parser.add_argument("command", choices=["init"], help="init：建表建索引（幂等）")
    args = parser.parse_args()
    if args.command == "init":
        init_db()
        print(f"database initialized at {db_path()}")
