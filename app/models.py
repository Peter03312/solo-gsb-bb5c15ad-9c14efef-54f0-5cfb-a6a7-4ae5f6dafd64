"""Pydantic 请求/响应模型与领域常量。

时间一律为 UTC：
* 证书有效期为左闭右开区间 ``[valid_from_utc, valid_to_utc)``；
* 允许值范围 ``[value_min, value_max]`` 为闭区间；
* 结论代码：``UNVERIFIED=0``（无匹配）、``PASS=1``（唯一匹配且在范围内）、
  ``FAIL=2``（唯一匹配但超限）、``REVIEW=3``（多份匹配，待复核）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---- 动作与结论常量 -------------------------------------------------------

ACTION_CERT_SUBMIT = "certificate.submit"
ACTION_CERT_CORRECT = "certificate.correct"
ACTION_CERT_REVOKE = "certificate.revoke"
ACTION_READING_SUBMIT = "reading.submit"

CONCLUSION_UNVERIFIED = 0
CONCLUSION_PASS = 1
CONCLUSION_FAIL = 2
CONCLUSION_REVIEW = 3

CONCLUSION_LABEL = {
    CONCLUSION_UNVERIFIED: "UNVERIFIED",
    CONCLUSION_PASS: "PASS",
    CONCLUSION_FAIL: "FAIL",
    CONCLUSION_REVIEW: "REVIEW",
}
CONCLUSION_TEXT = {
    CONCLUSION_UNVERIFIED: "未验证（无有效证书覆盖测量时间）",
    CONCLUSION_PASS: "合格（唯一匹配，数值在闭区间允许范围内）",
    CONCLUSION_FAIL: "超限（唯一匹配，数值超出闭区间允许范围）",
    CONCLUSION_REVIEW: "待复核（多份有效证书覆盖测量时间）",
}

# ---- 公共工具 -------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def norm_utc(dt: datetime, field: str) -> datetime:
    """要求带时区，统一换算到 UTC。"""
    if dt.tzinfo is None:
        raise ValueError(f"{field} 必须带时区偏移（UTC 请使用 'Z' 或 '+00:00'）")
    return dt.astimezone(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    """固定微秒精度的 UTC ISO 文本，保证按字典序可比较。"""
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


# ---- 证书请求 -------------------------------------------------------------


class CertificatePayload(BaseModel):
    """校准证书内容（新提交 / 纠正均用此结构）。"""

    model_config = ConfigDict(extra="forbid")

    cert_no: str = Field(min_length=1, description="证书编号")
    device_id: str = Field(min_length=1, description="设备编号")
    valid_from_utc: datetime = Field(description="UTC 有效期起（含）")
    valid_to_utc: datetime = Field(description="UTC 有效期止（不含）")
    value_min: float = Field(description="允许值范围下界（闭区间）")
    value_max: float = Field(description="允许值范围上界（闭区间）")

    @field_validator("valid_from_utc", "valid_to_utc")
    @classmethod
    def _tz(cls, v: datetime, info: Any) -> datetime:
        return norm_utc(v, info.field_name)

    @field_validator("value_min", "value_max")
    @classmethod
    def _finite(cls, v: float, info: Any) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{info.field_name} 必须为有限数值")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.valid_from_utc >= self.valid_to_utc:
            raise ValueError("valid_from_utc 必须早于 valid_to_utc（左闭右开有效期）")
        if self.value_min > self.value_max:
            raise ValueError("value_min 不能大于 value_max（闭区间允许值范围）")


class CertificateSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=1, description="请求标识（也可用 X-Request-ID 头）")
    certificate: CertificatePayload


class CertificateCorrectRequest(BaseModel):
    """以新内容纠正既有证书；纠正后旧证书在新版本立即失效，由新行接续。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=1)
    certificate: CertificatePayload


class CertificateRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=1)
    cert_no: str | None = Field(default=None, description="可省略，以路径参数为准")
    reason: str | None = Field(default=None, description="撤销原因，可选")


# ---- 读数请求 -------------------------------------------------------------


class ReadingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reading_no: str = Field(min_length=1, description="读数编号")
    device_id: str = Field(min_length=1, description="设备编号")
    measured_at_utc: datetime = Field(description="UTC 测量时间")
    value: float = Field(description="测量数值")

    @field_validator("measured_at_utc")
    @classmethod
    def _tz(cls, v: datetime, info: Any) -> datetime:
        return norm_utc(v, info.field_name)

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float, info: Any) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"{info.field_name} 必须为有限数值")
        return v


class ReadingSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str | None = Field(default=None, min_length=1)
    reading: ReadingPayload
