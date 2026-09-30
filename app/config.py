"""运行期配置，全部来自环境变量，均有默认值。"""
from __future__ import annotations

import os


def db_path() -> str:
    """SQLite 数据库文件路径，默认 ./data/trace.db。"""
    return os.getenv("TRACE_DB_PATH", os.path.join("data", "trace.db"))
