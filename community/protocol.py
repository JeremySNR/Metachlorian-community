"""An explicit wire allowlist. Never serialise a library record or raw analyser output."""
from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# No identities, locations from EXIF, human edits, arbitrary tags, rights, filenames or model errors.
FIELDS = frozenset({
    "content.caption", "content.summary", "content.setting", "content.time_of_day", "content.weather", "content.season",
    "content.objects", "content.activities", "content.concepts", "content.ocr_text",
    "semantic.topics", "semantic.mood", "semantic.suggested_uses", "semantic.summary",
    "camera.movement", "camera.shot_size", "camera.angle", "camera.speed_effect", "shot.role",
    "pacing.pace", "pacing.motion_energy", "pacing.audio_energy", "audio.classes", "audio.transcript", "audio.language",
    "audio.has_audio", "audio.speech_ratio", "quality.usable", "quality.flags", "quality.stability",
    "structure.edit_type", "structure.shot_count", "structure.cuts_per_minute", "structure.avg_shot_length",
})
Source = Literal["fusion", "rollup", "speech", "ocr", "motion", "audio", "quality", "visual_tags"]
Short = Annotated[str, Field(max_length=200)]
Text = Annotated[str, Field(max_length=12000)]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Signal(WireModel):
    value: Text | float | int | bool | list[Short]
    source: Source
    confidence: float | None = Field(default=None, ge=0, le=1)
    model_version: Annotated[str, Field(max_length=80)]


class Fields(WireModel):
    fields: dict[str, Signal] = Field(default_factory=dict, max_length=len(FIELDS))

    @field_validator("fields")
    @classmethod
    def allowed(cls, value):
        if set(value) - FIELDS:
            raise ValueError("field is outside the community allowlist")
        if any(isinstance(s.value, list) and len(s.value) > 30 for s in value.values()):
            raise ValueError("at most 30 terms per field")
        return value


class Shot(Fields):
    start_s: float = Field(ge=0, le=604800)
    end_s: float = Field(gt=0, le=604800)

    @model_validator(mode="after")
    def span(self):
        if self.end_s <= self.start_s:
            raise ValueError("end_s must follow start_s")
        return self


class Moment(WireModel):
    kind: Literal["speech", "text"]
    start_s: float = Field(ge=0, le=604800)
    end_s: float = Field(gt=0, le=604800)
    text: Text
    source: Literal["speech", "ocr"]
    model_version: Annotated[str, Field(max_length=80)]

    @model_validator(mode="after")
    def span(self):
        if self.end_s <= self.start_s:
            raise ValueError("end_s must follow start_s")
        return self


class Contribution(Fields):
    schema_version: Literal[1] = 1
    video_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{11}$")]
    shots: list[Shot] = Field(min_length=1, max_length=2000)
    moments: list[Moment] = Field(default_factory=list, max_length=4000)


def youtube_url(video_id: str, start_s: float | None = None) -> str:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("invalid YouTube video id")
    return f"https://www.youtube.com/watch?v={video_id}" + (f"&t={int(start_s)}s" if start_s is not None else "")


def validate_endpoint(value: str) -> str:
    """HTTPS in production; explicit loopback HTTP for local development. No credentials or URL parameters."""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("community_url must be a URL or empty")
    if not value:
        return value
    u = urlparse(value)
    if (not u.hostname or u.username or u.password or u.query or u.fragment or
            (u.scheme != "https" and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")))):
        raise ValueError("community_url needs HTTPS (HTTP is allowed only on loopback), without credentials, query or fragment")
    _ = u.port  # validate the port
    return value.rstrip("/")
