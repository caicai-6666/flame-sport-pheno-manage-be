"""定义管理端赛季结算查询与积分发放接口的数据结构。"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

SeasonUserId = Annotated[int, Field(gt=0)]


class SettlingSeasonOverviewResponse(BaseModel):
    season_id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int
    status: Literal[2]
    season_user_ids: list[int]


class SettlementParticipantsRequest(BaseModel):
    season_user_ids: list[SeasonUserId] = Field(
        min_length=1,
        max_length=1000,
    )


class SettlementProjectProgressResponse(BaseModel):
    project_id: int
    project_name: str
    completion_progress: float = Field(ge=0, le=1)


class SettlementParticipantResponse(BaseModel):
    season_user_id: int
    user_id: str
    username: str
    department_name: str
    avatar_url: str | None
    level_name: str
    projects: list[SettlementProjectProgressResponse]
    final_points: int | None = Field(default=None, ge=0)
    points_issued: bool


class SettlementPendingFinalReviewProofResponse(BaseModel):
    proof_record_id: int
    season_user_id: int
    project_id: int
    image_url: str
    created_at: datetime
    proof_date: date
    note: str | None
    review_comment: str | None


class IssueSeasonPointsRequest(BaseModel):
    season_user_id: SeasonUserId


class IssueSeasonPointsResponse(BaseModel):
    season_user_id: int
    final_points: int = Field(ge=0)
    points_issued: Literal[True]
    issued_now: bool


class SeasonCompletionResponse(BaseModel):
    season_id: int
    participant_count: int = Field(ge=0)
    rejected_proof_count: int = Field(ge=0)
    finalized_user_count: int = Field(ge=0)
    issued_user_count: int = Field(ge=0)
    season_ended: Literal[True]
