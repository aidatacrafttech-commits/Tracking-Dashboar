from __future__ import annotations

from pydantic import Field, field_validator

from app.core.time import validate_timezone_name
from app.schemas.common import ApiModel, InputModel


class SettingsRead(ApiModel):
    display_timezone: str
    supported_currencies: list[str]  # from environment; read-only here


class SettingsUpdate(InputModel):
    display_timezone: str = Field(min_length=1, max_length=64, examples=["Asia/Kolkata"])

    @field_validator("display_timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        return validate_timezone_name(value)
