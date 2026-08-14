"""定义运动凭证查询与终审接口的数据结构。"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


class PendingFinalReviewProofResponse(BaseModel):
    id: int
    project_id: int
    image_url: str
    created_at: datetime
    proof_date: date
    note: str | None
    review_comment: str | None


class FinalReviewRequest(BaseModel):
    proof_record_id: int = Field(gt=0)
    review_comment: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ] | None
    decision: Literal["approved", "rejected"]


class FinalReviewResponse(BaseModel):
    proof_record_id: int
    review_status: str
    review_comment: str | None
    rolled_back_progress: float = Field(ge=0, le=1)
    backfilled_progress: float = Field(ge=0, le=1)
    completion_progress: float | None = Field(default=None, ge=0, le=1)
