"""
Project Atlas - API contract
AT-26-L1-002  |  FR-07 at the HTTP boundary

The same principle the ingestion service applies to MQTT payloads applies here:
validate at the boundary, declaratively, before anything reaches the database.
The client is untrusted in exactly the way a device is untrusted.

These models are also the source of the generated OpenAPI document. Report 8.9
records ATL-M1-008 as a design-stage contract and says any divergence between
it and the generated specification is recorded in the as-built revision rather
than silently corrected - so these names deliberately follow the issued
specification, and where they cannot, the difference is a documented deviation.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import PASSWORD_MIN_LENGTH

Role = Literal["administrator", "operator", "viewer"]
CreatableRole = Literal["operator", "viewer"]
Severity = Literal["info", "warning", "critical"]
AlarmState = Literal["active", "acknowledged", "cleared"]


# --------------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str


class PublicRegistration(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=256)

    @field_validator("email")
    @classmethod
    def valid_email_shape(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("valid email is required")
        return value

    @field_validator("password")
    @classmethod
    def not_trivially_weak(cls, value: str) -> str:
        if len(set(value)) < 5:
            raise ValueError("password is too repetitive")
        return value


class EmailCodeRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return value.strip().lower()


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return value.strip().lower()


class ResetPasswordRequest(EmailCodeRequest):
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=256)

    @field_validator("new_password")
    @classmethod
    def reset_password_not_weak(cls, value: str) -> str:
        if len(set(value)) < 5:
            raise ValueError("password is too repetitive")
        return value


class MessageOut(BaseModel):
    message: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    email: Optional[str] = None
    role: Role
    is_active: bool
    created_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=256)
    email: Optional[str] = Field(default=None, max_length=254)
    role: CreatableRole

    @field_validator("password")
    @classmethod
    def not_trivially_weak(cls, v: str) -> str:
        # Length is the dominant factor and is enforced by the field. This
        # rejects only the failure mode length alone permits: a single
        # repeated character. Composition rules beyond that are not applied,
        # per NIST SP 800-63B, which recommends against them.
        if len(set(v)) < 5:
            raise ValueError("password is too repetitive")
        return v


# ------------------------------------------------------------------ devices
class DeviceOut(BaseModel):
    id: int
    device_uid: str
    name: str
    site: str
    location: Optional[str] = None
    status: str
    last_seen_at: Optional[datetime] = None
    created_at: datetime
    broker_provisioned: bool


class DeviceDetail(DeviceOut):
    mqtt_username: str
    sample_interval_ms: Optional[int] = None
    enabled_metrics: list[str] = []


class DeviceCreate(BaseModel):
    device_uid: str = Field(min_length=3, max_length=64,
                            pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    name: str = Field(min_length=1, max_length=128)
    site: str = Field(default="lab-a", max_length=64,
                      pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    location: Optional[str] = Field(default=None, max_length=256)
    enabled_metrics: list[str] = Field(default_factory=list, max_length=32)
    sample_interval_ms: int = Field(default=1000, ge=100, le=3_600_000)


class DeviceRegistered(BaseModel):
    """
    The secret appears in this response and nowhere else, ever. It is not
    stored by the platform in any form - report 8.7 places device credential
    material in the broker, not the application database.
    """
    device: DeviceOut
    mqtt_username: str
    mqtt_password: str = Field(description="Shown once. Not recoverable.")
    broker_provisioned: bool
    provisioning_note: Optional[str] = None


class DeviceConfigUpdate(BaseModel):
    sample_interval_ms: Optional[int] = Field(default=None, ge=100, le=3_600_000)
    enabled_metrics: Optional[list[str]] = Field(default=None, max_length=32)


class ThresholdUpdate(BaseModel):
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    max_rate_of_change: Optional[float] = Field(default=None, gt=0)
    z_window: Optional[int] = Field(default=None, ge=10, le=10_000)
    z_sigma: Optional[float] = Field(default=None, gt=0, le=10)
    confirm_samples: Optional[int] = Field(default=None, ge=1, le=100)

    @field_validator("max_value")
    @classmethod
    def ordered(cls, v, info):
        lo = info.data.get("min_value")
        if v is not None and lo is not None and v <= lo:
            raise ValueError("max_value must be greater than min_value")
        return v


class ThresholdOut(BaseModel):
    device_id: int
    metric: str
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    max_rate_of_change: Optional[float] = None
    z_window: Optional[int] = None
    z_sigma: Optional[float] = None
    confirm_samples: Optional[int] = None


# ---------------------------------------------------------------- telemetry
class TelemetryPoint(BaseModel):
    time: datetime
    device_id: int
    metric: str
    value: float
    quality: str


class AggregatePoint(BaseModel):
    bucket: datetime
    device_id: int
    metric: str
    avg_value: float
    min_value: float
    max_value: float
    sample_count: int


class EventOut(BaseModel):
    id: int
    device_id: int
    severity: Severity
    detector: str
    metric: Optional[str] = None
    observed_value: Optional[float] = None
    message: str
    occurred_at: datetime


class AlarmOut(BaseModel):
    id: int
    event_id: int
    device_id: int
    state: AlarmState
    severity: Severity
    raised_at: datetime
    acknowledged_by: Optional[str] = None
    cleared_at: Optional[datetime] = None


class AuditOut(BaseModel):
    id: int
    user_id: Optional[int] = None
    username: Optional[str] = None
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
    ip_address: Optional[str] = None
    occurred_at: datetime


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: str
    version: str
