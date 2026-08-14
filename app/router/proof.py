"""提供管理端凭证查询与后续终审接口。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.db.session import DatabaseSession
from app.schemas.proof import (
    FinalReviewRequest,
    FinalReviewResponse,
    PendingFinalReviewProofResponse,
)
from app.services.proofs import (
    ProofFinalReviewConflictError,
    ProofForFinalReviewNotFoundError,
    ProofProgressConsistencyError,
    list_pending_final_review_proofs,
    record_proof_final_review,
)

router = APIRouter(prefix="/proof", tags=["proof"])


# 接收赛季用户记录标识，并返回该用户所有有效的待终审凭证。
@router.get(
    "/pending-final-review",
    response_model=list[PendingFinalReviewProofResponse],
    summary="查询待终审凭证",
)
async def get_pending_final_review_proofs(
    session: DatabaseSession,
    season_user_id: Annotated[
        int,
        Query(gt=0, description="待查询的赛季用户记录 ID"),
    ],
) -> list[PendingFinalReviewProofResponse]:
    proofs = await list_pending_final_review_proofs(
        session,
        season_user_id,
    )
    return [
        PendingFinalReviewProofResponse.model_validate(
            proof,
            from_attributes=True,
        )
        for proof in proofs
    ]


# 接收凭证终审命令，并把状态冲突和进度一致性问题映射为安全 HTTP 响应。
@router.post(
    "/final-review",
    response_model=FinalReviewResponse,
    summary="记录凭证终审结果",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "凭证不存在或已失效"},
        status.HTTP_409_CONFLICT: {
            "description": "凭证状态或项目进度不允许完成终审"
        },
    },
)
async def submit_proof_final_review(
    session: DatabaseSession,
    request: FinalReviewRequest,
) -> FinalReviewResponse:
    try:
        result = await record_proof_final_review(
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
