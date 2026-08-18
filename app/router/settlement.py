"""集中承载结算赛季查询和后续结算操作接口。"""

from fastapi import APIRouter, HTTPException, status

from app.db.session import DatabaseSession
from app.schemas.proof import FinalReviewRequest, FinalReviewResponse
from app.schemas.settlement import (
    IssueSeasonPointsRequest,
    IssueSeasonPointsResponse,
    SeasonCompletionResponse,
    SettlementParticipantResponse,
    SettlementPendingFinalReviewProofResponse,
    SettlementParticipantsRequest,
    SettlingSeasonOverviewResponse,
)
from app.services.proofs import (
    ProofFinalReviewConflictError,
    ProofForFinalReviewNotFoundError,
    ProofNotInSettlingSeasonError,
    ProofProgressConsistencyError,
    record_settlement_proof_final_review,
)
from app.services.season_settlements import (
    MultipleSettlingSeasonsError,
    SeasonCompletionConflictError,
    SeasonCompletionConsistencyError,
    SeasonPointBalanceOverflowError,
    SeasonPointIssuanceConsistencyError,
    SeasonPointIssuanceNotAllowedError,
    SeasonPointsNotFinalizedError,
    SettlementParticipantNotFoundError,
    SettlementProgressConsistencyError,
    SettlingSeasonNotFoundError,
    complete_settling_season,
    get_settlement_participant_details,
    get_settlement_pending_final_review_proofs,
    get_settling_season_overview,
    issue_season_points,
)

router = APIRouter(prefix="/settlement", tags=["settlement"])


# 返回唯一结算赛季及正式参赛记录主键，并把一致性异常转换为稳定 HTTP 响应。
@router.get(
    "/current",
    response_model=SettlingSeasonOverviewResponse,
    summary="获取当前结算赛季基本信息",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "当前没有结算中赛季"},
        status.HTTP_409_CONFLICT: {"description": "存在多个结算中赛季"},
    },
)
async def get_current_settlement(
    session: DatabaseSession,
) -> SettlingSeasonOverviewResponse:
    try:
        overview = await get_settling_season_overview(session)
    except SettlingSeasonNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="当前没有结算中的赛季",
        ) from error
    except MultipleSettlingSeasonsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个结算中赛季，无法确定当前结算赛季",
        ) from error
    return SettlingSeasonOverviewResponse.model_validate(
        overview,
        from_attributes=True,
    )


# 按赛季用户主键批量返回结算展示与发放字段，并保持请求首次出现顺序。
@router.post(
    "/participants",
    response_model=list[SettlementParticipantResponse],
    summary="批量获取结算用户详情",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "当前没有结算中赛季"},
        status.HTTP_409_CONFLICT: {"description": "存在多个结算中赛季"},
    },
)
async def get_settlement_participants(
    session: DatabaseSession,
    request: SettlementParticipantsRequest,
) -> list[SettlementParticipantResponse]:
    try:
        participants = await get_settlement_participant_details(
            session,
            request.season_user_ids,
        )
    except SettlingSeasonNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="当前没有结算中的赛季",
        ) from error
    except MultipleSettlingSeasonsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个结算中赛季，无法确定当前结算赛季",
        ) from error
    return [
        SettlementParticipantResponse.model_validate(
            participant,
            from_attributes=True,
        )
        for participant in participants
    ]


# 返回唯一结算中赛季的全部待终审凭证，并附带参赛记录主键供前端关联用户。
@router.get(
    "/pending-final-reviews",
    response_model=list[SettlementPendingFinalReviewProofResponse],
    summary="获取结算赛季全部待终审凭证",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "当前没有结算中赛季"},
        status.HTTP_409_CONFLICT: {"description": "存在多个结算中赛季"},
    },
)
async def get_settlement_pending_final_reviews(
    session: DatabaseSession,
) -> list[SettlementPendingFinalReviewProofResponse]:
    try:
        proofs = await get_settlement_pending_final_review_proofs(session)
    except SettlingSeasonNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="当前没有结算中的赛季",
        ) from error
    except MultipleSettlingSeasonsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个结算中赛季，无法确定当前结算赛季",
        ) from error
    return [
        SettlementPendingFinalReviewProofResponse.model_validate(
            proof,
            from_attributes=True,
        )
        for proof in proofs
    ]


# 对当前结算赛季执行终审，并在同一事务内关闭资格和尝试自动定分。
@router.post(
    "/final-review",
    response_model=FinalReviewResponse,
    summary="记录结算赛季凭证终审结果",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "凭证不存在或已失效"},
        status.HTTP_409_CONFLICT: {
            "description": "凭证不属于结算赛季或当前状态不能终审"
        },
    },
)
async def submit_settlement_final_review(
    session: DatabaseSession,
    request: FinalReviewRequest,
) -> FinalReviewResponse:
    try:
        result = await record_settlement_proof_final_review(
            session,
            request.proof_record_id,
            request.review_comment,
            request.decision,
        )
    except ProofForFinalReviewNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="凭证不存在或已失效",
        ) from error
    except ProofNotInSettlingSeasonError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="凭证不属于当前结算赛季",
        ) from error
    except ProofFinalReviewConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="凭证已完成终审或当前状态不允许终审",
        ) from error
    except ProofProgressConsistencyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="凭证贡献与项目进度不一致，无法完成终审",
        ) from error
    return FinalReviewResponse.model_validate(result, from_attributes=True)


# 接收单个参赛记录并原子写入赛季奖励流水，重复请求不重复增加用户积分。
@router.post(
    "/issue-points",
    response_model=IssueSeasonPointsResponse,
    summary="发放单个用户的赛季积分",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "正式参赛记录不存在"},
        status.HTTP_409_CONFLICT: {
            "description": "尚未定分、赛季状态或积分数据不允许发放"
        },
    },
)
async def issue_settlement_points(
    session: DatabaseSession,
    request: IssueSeasonPointsRequest,
) -> IssueSeasonPointsResponse:
    try:
        result = await issue_season_points(
            session,
            request.season_user_id,
        )
    except SettlementParticipantNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="正式赛季参赛记录不存在",
        ) from error
    except SeasonPointsNotFinalizedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该用户尚未完成赛季定分",
        ) from error
    except SeasonPointIssuanceNotAllowedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该赛季当前不允许发放积分",
        ) from error
    except SeasonPointBalanceOverflowError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="发放后的用户积分余额超出允许范围",
        ) from error
    except SeasonPointIssuanceConsistencyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="积分数据不一致，无法完成发放",
        ) from error
    return IssueSeasonPointsResponse.model_validate(
        result,
        from_attributes=True,
    )


# 一键原子收口唯一结算赛季，并把一致性失败转换为不会泄露内部细节的响应。
@router.post(
    "/complete",
    response_model=SeasonCompletionResponse,
    summary="一键完成当前赛季结算",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "当前没有结算中赛季"},
        status.HTTP_409_CONFLICT: {
            "description": "赛季状态、进度或积分数据不允许完成结算"
        },
    },
)
async def complete_current_settlement(
    session: DatabaseSession,
) -> SeasonCompletionResponse:
    try:
        result = await complete_settling_season(session)
    except SettlingSeasonNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="当前没有结算中的赛季",
        ) from error
    except MultipleSettlingSeasonsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="存在多个结算中赛季，无法确定当前结算赛季",
        ) from error
    except SeasonPointBalanceOverflowError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="发放后的用户积分余额超出允许范围",
        ) from error
    except (
        SeasonCompletionConflictError,
        SeasonCompletionConsistencyError,
        SettlementProgressConsistencyError,
        SettlementParticipantNotFoundError,
        SeasonPointsNotFinalizedError,
        SeasonPointIssuanceNotAllowedError,
        SeasonPointIssuanceConsistencyError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="赛季结算数据不一致，无法完成一键结算",
        ) from error
    return SeasonCompletionResponse.model_validate(
        result,
        from_attributes=True,
    )
