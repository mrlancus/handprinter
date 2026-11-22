"""Pydantic request models: the explicit API contract for state-changing endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

class BoxIn(BaseModel):
    x: float
    y: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)


class BatchIn(BaseModel):
    source_type: Literal["paper", "ipad"] = "paper"
    session: str | None = Field(default=None, max_length=80)
    captured_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    style_era: Literal["current", "older"] = "current"
    note: str | None = Field(default=None, max_length=300)
    detect: bool = True


class SourcePatch(BaseModel):
    source_type: Literal["paper", "ipad"] | None = None
    session: str | None = Field(default=None, max_length=80)
    captured_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    style_era: Literal["current", "older"] | None = None
    group_with: str | None = None


class ProcessingIn(BaseModel):
    corners: list[list[float]] | None = Field(default=None, min_length=4, max_length=4)
    rotation: float = 0
    lighting: bool = False


class OmitIn(BaseModel):
    processing_rev: int
    box: BoxIn
    reason: Literal["diagram", "printed", "other_author", "scribble", "other"] = "diagram"


class DetectIn(BaseModel):
    replace: bool = False


class RegionCreate(BaseModel):
    processing_rev: int
    kind: Literal["line", "word", "block", "mark"]
    cls: str = "handwriting"
    box: BoxIn
    parent_id: str | None = None
    text: str | None = Field(default=None, max_length=2000)


class RegionPatch(BaseModel):
    expected_revision: int | None = None
    processing_rev: int | None = None
    text: str | None = Field(default=None, max_length=2000)
    clear_text: bool = False
    box: BoxIn | None = None
    cls: str | None = None
    kind: Literal["line", "word", "block", "mark"] | None = None
    other_author: bool | None = None
    deferred: bool | None = None
    status: Literal["unprocessed", "proposed", "needs_review", "approved", "excluded"] | None = None
    reason: str | None = Field(default=None, max_length=300)


class RevisionIn(BaseModel):
    expected_revision: int | None = None


class SplitIn(BaseModel):
    at_x: float | None = None
    at_y: float | None = None  # split top/bottom instead (a box holding two lines of writing)
    texts: tuple[str | None, str | None] = (None, None)


class MergeIn(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=50)
    text: str | None = None


class ApplyProposalIn(BaseModel):
    proposal_id: str
    expected_revision: int | None = None


class MarkLinkIn(BaseModel):
    mark_id: str
    target_id: str | None = None
    span: tuple[int, int] | None = None
    unknown_target: bool = False
    unknown_original: bool = False
    note: str | None = Field(default=None, max_length=300)


class BeforeAfterIn(BaseModel):
    before_id: str
    after_id: str
    note: str | None = Field(default=None, max_length=300)


class RecognitionIn(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=500)
    include_overview: bool = False
    force: bool = False


class HeadlessIn(BaseModel):
    item_ids: list[str] | None = None


class SnapshotIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    policy: Literal["approved_only", "include_unreviewed"] = "approved_only"
    source_types: list[Literal["paper", "ipad"]] = ["paper"]
    style_eras: list[Literal["current", "older"]] = ["current"]
    sessions: list[str] | None = None
    holdout_sessions: list[str] = []
    seed: int = 1234
    unseen_word_fraction: float = Field(default=0.1, ge=0, le=0.5)
    include_maths: bool = True


class RunIn(BaseModel):
    adapter: str
    level: Literal["word", "line"] = "word"
    texts: list[str] | None = Field(default=None, max_length=200)
    seeds: list[int] | None = Field(default=None, max_length=10)
    style_profile_id: str | None = None
    snapshot_id: str | None = None
    kind: Literal["baseline", "generation"] = "baseline"
    options: dict[str, float] = {}


class StyleProfileIn(BaseModel):
    snapshot_id: str
    name: str = Field(min_length=1, max_length=80)
    count: int = Field(default=5, ge=1, le=20)


class TrialIn(BaseModel):
    level: Literal["word", "line"] = "word"


class RateIn(BaseModel):
    choice: Literal["left", "right", "tie", "neither"]


class RefIn(BaseModel):
    id: str


class RenderIn(BaseModel):
    composition: dict
    dpi: int = Field(default=300, ge=50, le=600)


class CompositionSave(BaseModel):
    composition: dict
    expected_revision: int | None = None


class LoginIn(BaseModel):
    token: str = Field(max_length=200)
