"""领域服务：版本化写入、请求标识幂等、证书匹配与读数结论。

所有写操作在单个 SQLite 事务内完成：先做全部校验（含“未知目标”“无效区间”
“标识复用但内容不同”），再追加版本与数据；任何一步失败整体回滚，不留部分变更。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import db as dbmod
from .models import (
    ACTION_CERT_CORRECT,
    ACTION_CERT_REVOKE,
    ACTION_CERT_SUBMIT,
    ACTION_READING_SUBMIT,
    CONCLUSION_FAIL,
    CONCLUSION_LABEL,
    CONCLUSION_PASS,
    CONCLUSION_REVIEW,
    CONCLUSION_TEXT,
    CONCLUSION_UNVERIFIED,
    fmt_utc,
    utc_now,
)


class ServiceError(Exception):
    """业务校验错误，映射为 HTTP 响应。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---- 基础工具 -------------------------------------------------------------


def _content_hash(action: str, payload: dict[str, Any]) -> str:
    """对“动作 + 去掉 request_id 的请求体”做稳定摘要。"""
    body = {k: v for k, v in payload.items() if k != "request_id"}
    raw = json.dumps({"action": action, "body": body}, sort_keys=True,
                     ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _begin(conn: sqlite3.Connection) -> None:
    # IMMEDIATE：第一时间拿写锁，串行化并发写，保证版本与幂等判定一致。
    conn.execute("BEGIN IMMEDIATE")


def _add_version(conn: sqlite3.Connection, action: str, request_id: str) -> int:
    cur = conn.execute(
        "INSERT INTO versions(action, request_id, created_at_utc) VALUES (?,?,?)",
        (action, request_id, fmt_utc(utc_now())),
    )
    return int(cur.lastrowid)


def _finish(conn: sqlite3.Connection, request_id: str, action: str,
            content_hash: str, version: int, response: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO requests(request_id, action, content_hash, new_version,"
        " response_json, created_at_utc) VALUES (?,?,?,?,?,?)",
        (request_id, action, content_hash, version,
         json.dumps(response, ensure_ascii=False), fmt_utc(utc_now())),
    )
    conn.commit()


def _replay(conn: sqlite3.Connection, request_id: str, content_hash: str) -> dict[str, Any] | None:
    """命中相同请求标识：同内容 -> 重放原响应；不同内容 -> 冲突报错。"""
    row = conn.execute(
        "SELECT content_hash, response_json FROM requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    if row["content_hash"] != content_hash:
        raise ServiceError(409, "REQUEST_ID_CONFLICT",
                           f"请求标识 {request_id} 已用于不同内容，标识不可复用")
    return json.loads(row["response_json"])


# ---- 视图转换 -------------------------------------------------------------


def _cert_view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cert_no": row["cert_no"],
        "device_id": row["device_id"],
        "valid_from_utc": row["valid_from_utc"],
        "valid_to_utc": row["valid_to_utc"],
        "value_min": row["value_min"],
        "value_max": row["value_max"],
        "version_valid_from": row["version_valid_from"],
        "version_valid_to": row["version_valid_to"],
        "state": row["state"],
        "superseded_by_cert": row["superseded_by_cert"],
    }


def _cert_brief(row: sqlite3.Row) -> dict[str, Any]:
    """结论中记录所依证书的最小信息（旧版本回溯时该行内容不变）。"""
    return {
        "cert_no": row["cert_no"],
        "valid_from_utc": row["valid_from_utc"],
        "valid_to_utc": row["valid_to_utc"],
        "value_min": row["value_min"],
        "value_max": row["value_max"],
    }


def _active_certs_at(conn: sqlite3.Connection, device_id: str, version: int) -> list[sqlite3.Row]:
    """版本 v 时该设备生效中的证书行。

    生效性只由行版本区间 ``[version_valid_from, version_valid_to)`` 判定，
    不能用 ``state`` 列：state 记录的是行的“终结方式”（当前状态），
    撤销后变为 REVOKED，但在旧版本（< version_valid_to）它仍然生效。
    """
    return list(conn.execute(
        "SELECT * FROM certificate_versions"
        " WHERE device_id = ?"
        " AND version_valid_from <= ?"
        " AND (version_valid_to IS NULL OR ? < version_valid_to)"
        " ORDER BY cert_no, version_valid_from",
        (device_id, version, version),
    ).fetchall())


def evaluate_reading(conn: sqlite3.Connection, device_id: str,
                     measured_at: str, value: float, version: int) -> dict[str, Any]:
    """按指定版本计算单个读数结论。

    匹配规则：设备相同，且测量时间落入证书 UTC 左闭右开有效期
    ``valid_from_utc <= t < valid_to_utc``。
    * 0 份：未验证 UNVERIFIED
    * 多份：待复核 REVIEW
    * 唯一：数值在闭区间 [value_min, value_max] 内合格 PASS，否则超限 FAIL
    """
    matches = [
        c for c in _active_certs_at(conn, device_id, version)
        if c["valid_from_utc"] <= measured_at < c["valid_to_utc"]
    ]
    if not matches:
        code = CONCLUSION_UNVERIFIED
        chosen = None
    elif len(matches) > 1:
        code = CONCLUSION_REVIEW
        chosen = None
    else:
        c = matches[0]
        chosen = _cert_brief(c)
        code = CONCLUSION_PASS if c["value_min"] <= value <= c["value_max"] else CONCLUSION_FAIL
    return {
        "conclusion_code": code,
        "conclusion": CONCLUSION_LABEL[code],
        "explanation": CONCLUSION_TEXT[code],
        "evaluated_at_version": version,
        "matched_certificates": [_cert_brief(c) for c in matches],
        "basis_certificate": chosen,
    }


def _device_affected(conn: sqlite3.Connection, device_id: str,
                     old_version: int, new_version: int) -> list[dict[str, Any]]:
    """对比设备读数在两个版本的结论，返回结论发生变化的读数（含前后结论）。"""
    rows = conn.execute(
        "SELECT * FROM readings WHERE device_id = ? ORDER BY reading_no", (device_id,)
    ).fetchall()
    affected: list[dict[str, Any]] = []
    for r in rows:
        before = evaluate_reading(conn, device_id, r["measured_at_utc"], r["value"], old_version)
        after = evaluate_reading(conn, device_id, r["measured_at_utc"], r["value"], new_version)
        if before["conclusion_code"] != after["conclusion_code"]:
            affected.append({
                "reading_no": r["reading_no"],
                "measured_at_utc": r["measured_at_utc"],
                "value": r["value"],
                "before": before,
                "after": after,
            })
    return affected


def _latest_cert_row(conn: sqlite3.Connection, cert_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM certificate_versions WHERE cert_no = ?"
        " ORDER BY version_valid_from DESC LIMIT 1",
        (cert_no,),
    ).fetchone()


def _latest_active_cert(conn: sqlite3.Connection, cert_no: str) -> sqlite3.Row | None:
    row = _latest_cert_row(conn, cert_no)
    return row if row is not None and row["state"] == "ACTIVE" else None


def _insert_cert_row(conn: sqlite3.Connection, p: dict[str, Any],
                     version: int, state: str = "ACTIVE",
                     superseded_by_cert: str | None = None) -> None:
    conn.execute(
        "INSERT INTO certificate_versions(cert_no, device_id, valid_from_utc, valid_to_utc,"
        " value_min, value_max, version_valid_from, version_valid_to, state, superseded_by_cert)"
        " VALUES (?,?,?,?,?,?,?,NULL,?,?)",
        (p["cert_no"], p["device_id"], fmt_utc(p["valid_from_utc"]), fmt_utc(p["valid_to_utc"]),
         float(p["value_min"]), float(p["value_max"]), version, state, superseded_by_cert),
    )


# ---- 写：证书提交 ---------------------------------------------------------


def submit_certificate(conn: sqlite3.Connection, request_id: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
    cert = payload["certificate"]
    p = cert if isinstance(cert, dict) else cert.model_dump()
    content_hash = _content_hash(ACTION_CERT_SUBMIT, payload)
    try:
        _begin(conn)
        replay = _replay(conn, request_id, content_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay

        latest = _latest_cert_row(conn, p["cert_no"])
        if latest is not None:
            if latest["state"] == "ACTIVE":
                raise ServiceError(409, "CERT_ALREADY_ACTIVE",
                                   f"证书 {p['cert_no']} 已有生效版本"
                                   f"（v{latest['version_valid_from']}），请使用纠正或撤销")
            # 历史不可覆盖：撤销/被纠正的编号不能以“提交”方式复活。
            raise ServiceError(409, "CERT_NOT_ACTIVE",
                               f"证书 {p['cert_no']} 已{('撤销' if latest['state'] == 'REVOKED' else '被新版本纠正')}，"
                               "不能重新提交；请使用新证书编号")

        version = _add_version(conn, ACTION_CERT_SUBMIT, request_id)
        _insert_cert_row(conn, p, version)
        affected = _device_affected(conn, p["device_id"], version - 1, version)
        response = {
            "action": ACTION_CERT_SUBMIT,
            "request_id": request_id,
            "new_version": version,
            "certificate_no": p["cert_no"],
            "certificate_state": "ACTIVE",
            "certificate": {
                "cert_no": p["cert_no"],
                "device_id": p["device_id"],
                "valid_from_utc": fmt_utc(p["valid_from_utc"]),
                "valid_to_utc": fmt_utc(p["valid_to_utc"]),
                "value_min": float(p["value_min"]),
                "value_max": float(p["value_max"]),
            },
            "affected_readings_count": len(affected),
            "affected_readings": affected,
            "replayed": False,
        }
        _finish(conn, request_id, ACTION_CERT_SUBMIT, content_hash, version, response)
        return response
    except Exception:
        conn.rollback()
        raise


# ---- 写：证书纠正 ---------------------------------------------------------


def correct_certificate(conn: sqlite3.Connection, request_id: str,
                        payload: dict[str, Any]) -> dict[str, Any]:
    cert = payload["certificate"]
    p = cert if isinstance(cert, dict) else cert.model_dump()
    content_hash = _content_hash(ACTION_CERT_CORRECT, payload)
    try:
        _begin(conn)
        replay = _replay(conn, request_id, content_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay

        current = _latest_active_cert(conn, p["cert_no"])
        if current is None:
            raise ServiceError(404, "UNKNOWN_CERTIFICATE",
                               f"证书 {p['cert_no']} 不存在或已失效，无法纠正")
        if current["device_id"] != p["device_id"]:
            raise ServiceError(
                422, "CERT_DEVICE_IMMUTABLE",
                "纠正不能改变证书对应设备编号；如需变更设备，请撤销后以新证书编号提交",
            )

        version = _add_version(conn, ACTION_CERT_CORRECT, request_id)
        # 旧行：状态置为 SUPERSEDED，在新版本失效（左闭右开，故 version_valid_to = version）。
        conn.execute(
            "UPDATE certificate_versions SET state = 'SUPERSEDED', version_valid_to = ?,"
            " superseded_by_cert = ? WHERE cert_no = ? AND version_valid_from = ?",
            (version, p["cert_no"], p["cert_no"], current["version_valid_from"]),
        )
        _insert_cert_row(conn, p, version)
        affected = _device_affected(conn, p["device_id"], version - 1, version)
        response = {
            "action": ACTION_CERT_CORRECT,
            "request_id": request_id,
            "new_version": version,
            "certificate_no": p["cert_no"],
            "certificate_state": "ACTIVE",
            "superseded_version_from": current["version_valid_from"],
            "old_certificate": {
                "cert_no": current["cert_no"],
                "device_id": current["device_id"],
                "valid_from_utc": current["valid_from_utc"],
                "valid_to_utc": current["valid_to_utc"],
                "value_min": current["value_min"],
                "value_max": current["value_max"],
            },
            "new_certificate": {
                "cert_no": p["cert_no"],
                "device_id": p["device_id"],
                "valid_from_utc": fmt_utc(p["valid_from_utc"]),
                "valid_to_utc": fmt_utc(p["valid_to_utc"]),
                "value_min": float(p["value_min"]),
                "value_max": float(p["value_max"]),
            },
            "affected_readings_count": len(affected),
            "affected_readings": affected,
            "replayed": False,
        }
        _finish(conn, request_id, ACTION_CERT_CORRECT, content_hash, version, response)
        return response
    except Exception:
        conn.rollback()
        raise


# ---- 写：证书撤销 ---------------------------------------------------------


def revoke_certificate(conn: sqlite3.Connection, request_id: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
    cert_no = payload["cert_no"]
    reason = payload.get("reason")
    content_hash = _content_hash(ACTION_CERT_REVOKE, payload)
    try:
        _begin(conn)
        replay = _replay(conn, request_id, content_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay

        current = _latest_active_cert(conn, cert_no)
        if current is None:
            raise ServiceError(404, "UNKNOWN_CERTIFICATE",
                               f"证书 {cert_no} 不存在或已失效，无法撤销")

        version = _add_version(conn, ACTION_CERT_REVOKE, request_id)
        conn.execute(
            "UPDATE certificate_versions SET state = 'REVOKED', version_valid_to = ?"
            " WHERE cert_no = ? AND version_valid_from = ?",
            (version, cert_no, current["version_valid_from"]),
        )
        affected = _device_affected(conn, current["device_id"], version - 1, version)
        response = {
            "action": ACTION_CERT_REVOKE,
            "request_id": request_id,
            "new_version": version,
            "certificate_no": cert_no,
            "certificate_state": "REVOKED",
            "device_id": current["device_id"],
            "revoked_at_version": version,
            "reason": reason,
            "old_certificate": {
                "cert_no": current["cert_no"],
                "device_id": current["device_id"],
                "valid_from_utc": current["valid_from_utc"],
                "valid_to_utc": current["valid_to_utc"],
                "value_min": current["value_min"],
                "value_max": current["value_max"],
            },
            "affected_readings_count": len(affected),
            "affected_readings": affected,
            "replayed": False,
        }
        _finish(conn, request_id, ACTION_CERT_REVOKE, content_hash, version, response)
        return response
    except Exception:
        conn.rollback()
        raise


# ---- 写：读数提交 ---------------------------------------------------------


def submit_reading(conn: sqlite3.Connection, request_id: str,
                   payload: dict[str, Any]) -> dict[str, Any]:
    reading = payload["reading"]
    p = reading if isinstance(reading, dict) else reading.model_dump()
    content_hash = _content_hash(ACTION_READING_SUBMIT, payload)
    try:
        _begin(conn)
        replay = _replay(conn, request_id, content_hash)
        if replay is not None:
            conn.rollback()
            replay["replayed"] = True
            return replay

        dup = conn.execute(
            "SELECT 1 FROM readings WHERE reading_no = ?", (p["reading_no"],)
        ).fetchone()
        if dup is not None:
            raise ServiceError(409, "READING_ALREADY_EXISTS",
                               f"读数 {p['reading_no']} 已存在，读数历史不可覆盖")

        version = _add_version(conn, ACTION_READING_SUBMIT, request_id)
        measured_at = fmt_utc(p["measured_at_utc"])
        conn.execute(
            "INSERT INTO readings(reading_no, device_id, measured_at_utc, value,"
            " submitted_version, created_at_utc) VALUES (?,?,?,?,?,?)",
            (p["reading_no"], p["device_id"], measured_at, float(p["value"]),
             version, fmt_utc(utc_now())),
        )
        conclusion = evaluate_reading(
            conn, p["device_id"], measured_at, float(p["value"]), version
        )
        response = {
            "action": ACTION_READING_SUBMIT,
            "request_id": request_id,
            "new_version": version,
            "reading": {
                "reading_no": p["reading_no"],
                "device_id": p["device_id"],
                "measured_at_utc": measured_at,
                "value": float(p["value"]),
                "submitted_version": version,
            },
            "conclusion": conclusion,
            "replayed": False,
        }
        _finish(conn, request_id, ACTION_READING_SUBMIT, content_hash, version, response)
        return response
    except Exception:
        conn.rollback()
        raise


# ---- 读查询 ---------------------------------------------------------------


def get_reading_view(conn: sqlite3.Connection, reading_no: str,
                     at_version: int | None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM readings WHERE reading_no = ?", (reading_no,)).fetchone()
    if row is None:
        raise ServiceError(404, "UNKNOWN_READING", f"读数 {reading_no} 不存在")
    latest = dbmod.latest_version(conn)
    if at_version is None:
        at_version = latest
    if at_version < 0 or at_version > latest:
        raise ServiceError(404, "VERSION_NOT_FOUND",
                           f"版本 {at_version} 不存在（当前最新版本 {latest}）")
    conclusion = evaluate_reading(
        conn, row["device_id"], row["measured_at_utc"], row["value"], at_version
    )
    return {
        "reading_no": row["reading_no"],
        "device_id": row["device_id"],
        "measured_at_utc": row["measured_at_utc"],
        "value": row["value"],
        "submitted_version": row["submitted_version"],
        "at_version": at_version,
        "latest_version": latest,
        "conclusion": conclusion,
    }


def list_readings(conn: sqlite3.Connection, at_version: int | None,
                  device_id: str | None) -> dict[str, Any]:
    latest = dbmod.latest_version(conn)
    if at_version is None:
        at_version = latest
    if at_version < 0 or at_version > latest:
        raise ServiceError(404, "VERSION_NOT_FOUND",
                           f"版本 {at_version} 不存在（当前最新版本 {latest}）")
    sql = "SELECT * FROM readings"
    args: list[Any] = []
    if device_id:
        sql += " WHERE device_id = ?"
        args.append(device_id)
    sql += " ORDER BY measured_at_utc, reading_no"
    items = []
    for r in conn.execute(sql, args).fetchall():
        items.append({
            "reading_no": r["reading_no"],
            "device_id": r["device_id"],
            "measured_at_utc": r["measured_at_utc"],
            "value": r["value"],
            "submitted_version": r["submitted_version"],
            "conclusion": evaluate_reading(
                conn, r["device_id"], r["measured_at_utc"], r["value"], at_version
            ),
        })
    return {"at_version": at_version, "latest_version": latest,
            "device_id": device_id, "count": len(items), "readings": items}


def reading_timeline(conn: sqlite3.Connection, reading_no: str) -> dict[str, Any]:
    """读数结论在每个版本的取值，便于审计“撤销/纠正如何改变结论”。"""
    row = conn.execute("SELECT * FROM readings WHERE reading_no = ?", (reading_no,)).fetchone()
    if row is None:
        raise ServiceError(404, "UNKNOWN_READING", f"读数 {reading_no} 不存在")
    latest = dbmod.latest_version(conn)
    timeline = []
    for v in range(row["submitted_version"], latest + 1):
        c = evaluate_reading(conn, row["device_id"], row["measured_at_utc"], row["value"], v)
        timeline.append({
            "version": v,
            "conclusion_code": c["conclusion_code"],
            "conclusion": c["conclusion"],
            "basis_certificate": c["basis_certificate"],
        })
    return {
        "reading_no": reading_no,
        "device_id": row["device_id"],
        "measured_at_utc": row["measured_at_utc"],
        "value": row["value"],
        "submitted_version": row["submitted_version"],
        "latest_version": latest,
        "timeline": timeline,
    }


def get_certificate(conn: sqlite3.Connection, cert_no: str,
                    at_version: int | None) -> dict[str, Any]:
    latest = dbmod.latest_version(conn)
    if at_version is None:
        at_version = latest
    if at_version < 0 or at_version > latest:
        raise ServiceError(404, "VERSION_NOT_FOUND",
                           f"版本 {at_version} 不存在（当前最新版本 {latest}）")
    rows = conn.execute(
        "SELECT * FROM certificate_versions WHERE cert_no = ?"
        " AND version_valid_from <= ? ORDER BY version_valid_from",
        (cert_no, at_version),
    ).fetchall()
    if not rows:
        raise ServiceError(404, "UNKNOWN_CERTIFICATE",
                           f"证书 {cert_no} 在版本 {at_version} 不存在")
    visible = _cert_view(rows[-1])  # 该版本可见的最新一行
    # state 列是行的终结状态；查询版本落在行区间内时，当时状态仍为 ACTIVE。
    end = visible["version_valid_to"]
    if end is None or at_version < end:
        visible["state"] = "ACTIVE"
    return {"at_version": at_version, "latest_version": latest,
            "certificate": visible}


def list_certificates(conn: sqlite3.Connection, device_id: str | None) -> dict[str, Any]:
    sql = "SELECT * FROM certificate_versions"
    args: list[Any] = []
    if device_id:
        sql += " WHERE device_id = ?"
        args.append(device_id)
    sql += " ORDER BY cert_no, version_valid_from"
    rows = [_cert_view(r) for r in conn.execute(sql, args).fetchall()]
    return {"latest_version": dbmod.latest_version(conn),
            "device_id": device_id, "count": len(rows), "certificates": rows}


def list_versions(conn: sqlite3.Connection, limit: int, offset: int) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) c FROM versions").fetchone()["c"]
    rows = conn.execute(
        "SELECT version, action, request_id, created_at_utc FROM versions"
        " ORDER BY version DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {"count": len(rows), "total": total, "limit": limit, "offset": offset,
            "versions": [dict(r) for r in rows]}


def get_request_record(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, "UNKNOWN_REQUEST", f"请求标识 {request_id} 不存在")
    return {
        "request_id": row["request_id"],
        "action": row["action"],
        "content_hash": row["content_hash"],
        "new_version": row["new_version"],
        "created_at_utc": row["created_at_utc"],
        "response": json.loads(row["response_json"]),
    }
