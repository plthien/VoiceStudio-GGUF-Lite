"""Strict request validation; does not imply a mounted or ready provider."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .pocket_registry import resolve_model, MAX_INPUT_CHARS, SPEED_MIN, SPEED_MAX
from .pocket_segmentation import validate_text

class PocketSpeech(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["pocket-tts"] = "pocket-tts"
    language: str
    model_variant: str
    voice: str = Field(min_length=1, max_length=160)
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    response_format: Literal["wav"] = "wav"
    speed: float = Field(default=1.0, ge=SPEED_MIN, le=SPEED_MAX, allow_inf_nan=False)
    precision: Literal["fp32", "int8"] = "fp32"

    @field_validator("speed", mode="before")
    @classmethod
    def numeric_speed(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("SPEED_OUT_OF_RANGE")
        return value

    @field_validator("voice", "language", "model_variant")
    @classmethod
    def valid_string(cls, value):
        value.encode("utf-8", "strict")
        if not value.strip():
            raise ValueError("INVALID_REQUEST")
        return value

    @field_validator("input")
    @classmethod
    def valid_text(cls, value):
        validate_text(value)
        return value

    @model_validator(mode="after")
    def match_model(self):
        resolve_model(self.language, self.model_variant)
        return self

class PocketCancel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    cancel_token: str = Field(min_length=24, max_length=256)
