"""端到端 API 测试（httpx + FastAPI TestClient，临时 SQLite 文件）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "trace.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(db_file))
    dbmod.init_db(str(db_file))
    with TestClient(app) as c:
        yield c


CERT1 = {
    "request_id": "cert-1",
    "certificate": {
        "cert_no": "C-001", "device_id": "DEV-A",
        "valid_from_utc": "2026-01-01T00:00:00Z",
        "valid_to_utc": "2026-02-01T00:00:00Z",
        "value_min": 0.0, "value_max": 10.0,
    },
}


def _submit_cert(client, rid, **cert_overrides):
    cert = dict(CERT1["certificate"])
    cert.update(cert_overrides)
    return client.post("/api/v1/certificates",
                       json={"request_id": rid, "certificate": cert})


def test_health_and_empty(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["latest_version"] == 0


def test_write_requires_request_id(client):
    r = client.post("/api/v1/certificates", json={"certificate": CERT1["certificate"]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "REQUEST_ID_REQUIRED"


def test_invalid_intervals_rejected(client):
    # 有效期左闭右开：起 >= 止非法
    r = _submit_cert(client, "bad-1",
                     valid_from_utc="2026-02-01T00:00:00Z",
                     valid_to_utc="2026-02-01T00:00:00Z")
    assert r.status_code == 422
    # 闭区间范围：下界 > 上界非法
    r = _submit_cert(client, "bad-2", value_min=10, value_max=1)
    assert r.status_code == 422
    # 不带时区的时间非法
    r = _submit_cert(client, "bad-3", valid_from_utc="2026-01-01T00:00:00")
    assert r.status_code == 422
    # 失败不留任何版本
    assert client.get("/api/v1/versions").json()["total"] == 0


def test_cert_submit_and_idempotency(client):
    r = client.post("/api/v1/certificates", json=CERT1)
    assert r.status_code == 201
    body = r.json()
    assert body["new_version"] == 1
    assert body["replayed"] is False

    # 相同请求标识 + 相同内容：不生成新版本，重放原响应
    r2 = client.post("/api/v1/certificates", json=CERT1)
    assert r2.status_code == 201
    assert r2.json()["new_version"] == 1
    assert r2.json()["replayed"] is True

    # 标识复用但内容不同：409，且不产生新版本
    changed = {"request_id": "cert-1",
               "certificate": {**CERT1["certificate"], "value_max": 99.0}}
    r3 = client.post("/api/v1/certificates", json=changed)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert client.get("/healthz").json()["latest_version"] == 1

    # 头部请求标识同样可用
    r4 = _submit_cert(client, "cert-2", cert_no="C-002")
    assert r4.status_code == 201
    assert r4.json()["new_version"] == 2


def test_reading_conclusions_and_interval_boundaries(client):
    assert _submit_cert(client, "c1").json()["new_version"] == 1

    def reading(rid, t, v):
        return client.post("/api/v1/readings", json={
            "request_id": rid,
            "reading": {"reading_no": rid, "device_id": "DEV-A",
                        "measured_at_utc": t, "value": v},
        })

    # 有效期起点（含）+ 闭区间下界（含）=> 合格
    r = reading("R-LOW", "2026-01-01T00:00:00Z", 0.0)
    assert r.json()["conclusion"]["conclusion"] == "PASS"
    # 闭区间上界含
    assert reading("R-HIGH", "2026-01-15T00:00:00Z", 10.0
                   ).json()["conclusion"]["conclusion"] == "PASS"
    # 超范围
    assert reading("R-FAIL", "2026-01-15T00:00:00Z", 10.5
                   ).json()["conclusion"]["conclusion"] == "FAIL"
    # 有效期终点不含 => 未验证
    assert reading("R-END", "2026-02-01T00:00:00Z", 5.0
                   ).json()["conclusion"]["conclusion"] == "UNVERIFIED"
    # 有效期之前 => 未验证
    assert reading("R-BEFORE", "2025-12-31T23:59:59Z", 5.0
                   ).json()["conclusion"]["conclusion"] == "UNVERIFIED"
    # 其他设备 => 未验证
    assert reading("R-DEV", "2026-01-15T00:00:00Z", 5.0
                   ).json()  # default device override below
    r = client.post("/api/v1/readings", json={
        "request_id": "R-DEV2",
        "reading": {"reading_no": "R-DEV2", "device_id": "DEV-B",
                    "measured_at_utc": "2026-01-15T00:00:00Z", "value": 5.0}})
    assert r.json()["conclusion"]["conclusion"] == "UNVERIFIED"


def test_multiple_matching_certificates_means_review(client):
    assert _submit_cert(client, "c1").json()["new_version"] == 1
    # 与 C-001 在时间上重叠的第二份证书
    assert _submit_cert(client, "c2", cert_no="C-002",
                        valid_from_utc="2026-01-15T00:00:00Z",
                        valid_to_utc="2026-01-20T00:00:00Z").status_code == 201
    r = client.post("/api/v1/readings", json={
        "request_id": "R1",
        "reading": {"reading_no": "R1", "device_id": "DEV-A",
                    "measured_at_utc": "2026-01-16T00:00:00Z", "value": 5.0}})
    c = r.json()["conclusion"]
    assert c["conclusion"] == "REVIEW"
    assert len(c["matched_certificates"]) == 2


def test_correction_changes_conclusion_but_history_remains(client):
    assert _submit_cert(client, "c1").json()["new_version"] == 1
    r = client.post("/api/v1/readings", json={
        "request_id": "r1",
        "reading": {"reading_no": "R1", "device_id": "DEV-A",
                    "measured_at_utc": "2026-01-10T00:00:00Z", "value": 9.5}})
    assert r.json()["conclusion"]["conclusion"] == "PASS"
    reading_version = r.json()["new_version"]  # v2

    # 纠正：收紧范围到 [0, 9]
    corrected = {
        "request_id": "fix-1",
        "certificate": {**CERT1["certificate"], "value_min": 0.0, "value_max": 9.0},
    }
    r = client.post("/api/v1/certificates/C-001/correct", json=corrected)
    assert r.status_code == 200
    v3 = r.json()["new_version"]
    assert v3 == 3
    # 响应直接报告受影响读数的结论前后变化
    aff = r.json()["affected_readings"]
    assert aff[0]["reading_no"] == "R1"
    assert aff[0]["before"]["conclusion"] == "PASS"
    assert aff[0]["after"]["conclusion"] == "FAIL"

    # 当前结论已变为超限
    now = client.get("/api/v1/readings/R1").json()
    assert now["conclusion"]["conclusion"] == "FAIL"
    assert now["conclusion"]["basis_certificate"]["value_max"] == 9.0

    # 旧版本结论仍是合格，且所依证书为旧内容（value_max=10）
    old = client.get(f"/api/v1/readings/R1?at_version={reading_version}").json()
    assert old["conclusion"]["conclusion"] == "PASS"
    assert old["conclusion"]["basis_certificate"]["value_max"] == 10.0

    # 旧版本所见证书仍为生效的旧行
    old_cert = client.get("/api/v1/certificates/C-001?at_version=1").json()["certificate"]
    assert old_cert["state"] == "ACTIVE"
    assert old_cert["value_max"] == 10.0
    cur_cert = client.get("/api/v1/certificates/C-001").json()["certificate"]
    assert cur_cert["state"] == "ACTIVE"
    assert cur_cert["value_max"] == 9.0

    # 历史行仍可列出：旧行当前状态 SUPERSEDED，行版本区间在 v3 失效
    rows = client.get("/api/v1/certificates").json()["certificates"]
    c001 = [x for x in rows if x["cert_no"] == "C-001"]
    assert {x["state"] for x in c001} == {"SUPERSEDED", "ACTIVE"}
    old_row = [x for x in c001 if x["state"] == "SUPERSEDED"][0]
    assert old_row["version_valid_to"] == 3
    # 撤销/纠正后的证书在旧版本下当时仍是 ACTIVE（历史结论可原样重现）
    assert client.get("/api/v1/certificates/C-001?at_version=2"
                      ).json()["certificate"]["state"] == "ACTIVE"


def test_revocation_changes_conclusion_and_blocks_resubmit(client):
    assert _submit_cert(client, "c1").json()["new_version"] == 1
    client.post("/api/v1/readings", json={
        "request_id": "r1",
        "reading": {"reading_no": "R1", "device_id": "DEV-A",
                    "measured_at_utc": "2026-01-10T00:00:00Z", "value": 5.0}})

    r = client.post("/api/v1/certificates/C-001/revoke",
                    headers={"X-Request-ID": "rev-1"}, json={})
    assert r.status_code == 200
    v = r.json()["new_version"]
    assert r.json()["certificate_state"] == "REVOKED"
    assert r.json()["affected_readings"][0]["after"]["conclusion"] == "UNVERIFIED"

    # 当前：未验证；撤销前版本：仍合格
    assert client.get("/api/v1/readings/R1").json()["conclusion"]["conclusion"] == "UNVERIFIED"
    assert client.get("/api/v1/readings/R1?at_version=2"
                      ).json()["conclusion"]["conclusion"] == "PASS"
    cert_now = client.get("/api/v1/certificates/C-001").json()["certificate"]
    assert cert_now["state"] == "REVOKED"

    # 撤销幂等：同标识同内容重放
    again = client.post("/api/v1/certificates/C-001/revoke",
                        headers={"X-Request-ID": "rev-1"}, json={})
    assert again.json()["new_version"] == v
    assert again.json()["replayed"] is True

    # 再次撤销未知/已失效目标 -> 404
    r = client.post("/api/v1/certificates/C-001/revoke",
                    headers={"X-Request-ID": "rev-2"}, json={})
    assert r.status_code == 404
    # 撤销后不能以同编号重新提交
    r = _submit_cert(client, "c1-again")
    assert r.status_code == 409
    # 纠正一个不存在的证书 -> 404
    r = client.post("/api/v1/certificates/NOPE/correct", json={
        "request_id": "fix-x",
        "certificate": {**CERT1["certificate"], "cert_no": "NOPE"}})
    assert r.status_code == 404
    # 所有失败均无新版本产生（最新仍是撤销版本 v3）
    assert client.get("/healthz").json()["latest_version"] == v


def test_correction_cannot_change_device(client):
    assert _submit_cert(client, "c1").status_code == 201
    r = client.post("/api/v1/certificates/C-001/correct", json={
        "request_id": "fix-dev",
        "certificate": {**CERT1["certificate"], "device_id": "DEV-B"}})
    assert r.status_code == 422
    assert client.get("/api/v1/certificates/C-001").json()["certificate"]["device_id"] == "DEV-A"


def test_duplicate_reading_rejected(client):
    assert _submit_cert(client, "c1").status_code == 201
    payload = {"request_id": "r1",
               "reading": {"reading_no": "R1", "device_id": "DEV-A",
                           "measured_at_utc": "2026-01-10T00:00:00Z", "value": 5.0}}
    assert client.post("/api/v1/readings", json=payload).status_code == 201
    # 同读数编号、不同请求标识 => 拒绝，历史不可覆盖
    payload["request_id"] = "r1-retry"
    r = client.post("/api/v1/readings", json=payload)
    assert r.status_code == 409


def test_reading_timeline_and_version_listing(client):
    assert _submit_cert(client, "c1").json()["new_version"] == 1
    client.post("/api/v1/readings", json={
        "request_id": "r1",
        "reading": {"reading_no": "R1", "device_id": "DEV-A",
                    "measured_at_utc": "2026-01-10T00:00:00Z", "value": 50.0}})
    client.post("/api/v1/certificates/C-001/revoke",
                headers={"X-Request-ID": "rev1"}, json={})
    tl = client.get("/api/v1/readings/R1/timeline").json()["timeline"]
    seq = [(e["version"], e["conclusion"]) for e in tl]
    assert seq == [(2, "FAIL"), (3, "UNVERIFIED")]

    versions = client.get("/api/v1/versions").json()
    assert versions["total"] == 3
    actions = [v["action"] for v in versions["versions"]]
    assert actions == ["certificate.revoke", "reading.submit", "certificate.submit"]


def test_request_record_lookup(client):
    assert _submit_cert(client, "c1").status_code == 201
    rec = client.get("/api/v1/requests/c1").json()
    assert rec["new_version"] == 1
    assert rec["action"] == "certificate.submit"
    assert client.get("/api/v1/requests/missing").status_code == 404
