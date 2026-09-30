"""FastAPI HTTP 接口。

写入接口（均需请求标识，可用 ``X-Request-ID`` 头或 JSON 中的 ``request_id``）：
* POST /api/v1/certificates            提交校准证书
* POST /api/v1/certificates/{cert_no}/correct   纠正证书（旧证书新版本失效）
* POST /api/v1/certificates/{revoke}            撤销证书
* POST /api/v1/readings                提交读数

查询接口：
* GET  /api/v1/versions                版本流水
* GET  /api/v1/certificates[?device_id=]        证书全部历史行
* GET  /api/v1/certificates/{cert_no}?at_version=   指定版本所见证书
* GET  /api/v1/readings[?device_id=&at_version=]    指定版本的读数结论
* GET  /api/v1/readings/{reading_no}?at_version=
* GET  /api/v1/readings/{reading_no}/timeline
* GET  /api/v1/requests/{request_id}   请求标识（幂等记录）查询
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import db as dbmod
from . import service
from .models import (
    CertificateCorrectRequest,
    CertificateRevokeRequest,
    CertificateSubmitRequest,
    ReadingSubmitRequest,
)

app = FastAPI(
    title="实验室测量可信性追溯服务",
    description="校准证书与测量读数的版本化、可追溯、可复核 HTTP API",
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    # 容器启动即初始化数据库（建表幂等），也可先手动执行 python -m app.db init。
    dbmod.init_db()


def get_conn() -> Any:
    conn = dbmod.connect()
    try:
        yield conn
    finally:
        conn.close()


def resolve_request_id(body_id: str | None, x_request_id: str | None) -> str:
    rid = (x_request_id or "").strip() or (body_id or "").strip() or None
    if not rid:
        raise service.ServiceError(
            400, "REQUEST_ID_REQUIRED",
            "写入操作必须携带请求标识：使用 X-Request-ID 头或请求体 request_id 字段",
        )
    if body_id and x_request_id and body_id.strip() != x_request_id.strip():
        raise service.ServiceError(
            409, "REQUEST_ID_CONFLICT",
            "请求头 X-Request-ID 与请求体 request_id 不一致",
        )
    return rid


@app.exception_handler(service.ServiceError)
async def service_error_handler(_: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", tags=["system"])
def healthz(conn: Any = Depends(get_conn)) -> dict[str, Any]:
    latest = dbmod.latest_version(conn)
    return {"status": "ok", "latest_version": latest}


# ---- 写入 -----------------------------------------------------------------


@app.post("/api/v1/certificates", status_code=201, tags=["certificates"])
def api_submit_certificate(
    body: CertificateSubmitRequest,
    conn: Any = Depends(get_conn),
    x_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    request_id = resolve_request_id(body.request_id, x_request_id)
    return service.submit_certificate(conn, request_id, body.model_dump())


@app.post("/api/v1/certificates/{cert_no}/correct", status_code=200, tags=["certificates"])
def api_correct_certificate(
    cert_no: str,
    body: CertificateCorrectRequest,
    conn: Any = Depends(get_conn),
    x_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    request_id = resolve_request_id(body.request_id, x_request_id)
    if body.certificate.cert_no != cert_no:
        raise service.ServiceError(
            422, "CERT_NO_MISMATCH",
            f"路径证书编号 {cert_no} 与请求体 {body.certificate.cert_no} 不一致",
        )
    return service.correct_certificate(conn, request_id, body.model_dump())


@app.post("/api/v1/certificates/{cert_no}/revoke", status_code=200, tags=["certificates"])
def api_revoke_certificate(
    cert_no: str,
    body: CertificateRevokeRequest | None = None,
    conn: Any = Depends(get_conn),
    x_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    # 允许空 body 仅用头部携带请求标识。
    payload: dict[str, Any] = {"cert_no": cert_no, "reason": None}
    body_id: str | None = None
    if body is not None:
        if body.cert_no and body.cert_no != cert_no:
            raise service.ServiceError(
                422, "CERT_NO_MISMATCH",
                f"路径证书编号 {cert_no} 与请求体 {body.cert_no} 不一致",
            )
        payload["reason"] = body.reason
        body_id = body.request_id
    request_id = resolve_request_id(body_id, x_request_id)
    return service.revoke_certificate(conn, request_id, payload)


@app.post("/api/v1/readings", status_code=201, tags=["readings"])
def api_submit_reading(
    body: ReadingSubmitRequest,
    conn: Any = Depends(get_conn),
    x_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    request_id = resolve_request_id(body.request_id, x_request_id)
    return service.submit_reading(conn, request_id, body.model_dump())


# ---- 查询 -----------------------------------------------------------------


@app.get("/api/v1/versions", tags=["traceability"])
def api_versions(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.list_versions(conn, limit, offset)


@app.get("/api/v1/certificates", tags=["certificates"])
def api_list_certificates(
    device_id: str | None = Query(default=None),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.list_certificates(conn, device_id)


@app.get("/api/v1/certificates/{cert_no}", tags=["certificates"])
def api_get_certificate(
    cert_no: str,
    at_version: int | None = Query(default=None, ge=0, description="按指定版本查询（默认最新）"),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.get_certificate(conn, cert_no, at_version)


@app.get("/api/v1/readings", tags=["readings"])
def api_list_readings(
    at_version: int | None = Query(default=None, ge=0),
    device_id: str | None = Query(default=None),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.list_readings(conn, at_version, device_id)


@app.get("/api/v1/readings/{reading_no}", tags=["readings"])
def api_get_reading(
    reading_no: str,
    at_version: int | None = Query(default=None, ge=0, description="按指定提交版本查询（默认最新）"),
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.get_reading_view(conn, reading_no, at_version)


@app.get("/api/v1/readings/{reading_no}/timeline", tags=["readings"])
def api_reading_timeline(
    reading_no: str,
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.reading_timeline(conn, reading_no)


@app.get("/api/v1/requests/{request_id}", tags=["traceability"])
def api_get_request(
    request_id: str,
    conn: Any = Depends(get_conn),
) -> dict[str, Any]:
    return service.get_request_record(conn, request_id)
