"""编排管理端凭证查询、终审状态与项目进度调整用例。"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlmodel.ext.asyncio.session import AsyncSession

from app.repositories.notifications import (
    NotificationField,
    insert_notification,
)
from app.repositories.proofs import (
    PendingFinalReviewProof,
    ProofForFinalReview,
    ProofNotificationContext,
    fetch_locked_backfill_candidates,
    fetch_locked_project_progress,
    fetch_pending_final_review_proofs,
    fetch_proof_for_final_review,
    fetch_proof_notification_context,
    update_project_completion_progress,
    update_proof_final_review,
    update_proof_increase,
)

ZERO_PROGRESS = Decimal("0.0000")
PROOF_REJECTION_NOTIFICATION_TITLE = "运动凭证终审结果"
MISSING_REVIEW_COMMENT = "未填写"


class ProofForFinalReviewNotFoundError(RuntimeError):
    """凭证不存在、已失效或不具备可见性。"""


class ProofFinalReviewConflictError(RuntimeError):
    """凭证已经离开待终审状态，不能重复改变终审结论。"""


class ProofProgressConsistencyError(RuntimeError):
    """凭证贡献与用户项目进度不一致，终审事务必须停止。"""


@dataclass(frozen=True, slots=True)
class FinalReviewResult:
    proof_record_id: int
    review_status: str
    review_comment: str | None
    rolled_back_progress: Decimal
    backfilled_progress: Decimal
    completion_progress: Decimal | None


# 构造终审拒绝的固定顺序消息字段，空审核意见使用明确占位文本。
def build_proof_rejection_notification_fields(
    context: ProofNotificationContext,
    review_comment: str | None,
) -> tuple[NotificationField, ...]:
    return (
        NotificationField("审核结果", "未通过"),
        NotificationField("运动项目", context.project_name),
        NotificationField("凭证日期", context.proof_date.isoformat()),
        NotificationField(
            "审核意见",
            (
                review_comment
                if review_comment is not None
                else MISSING_REVIEW_COMMENT
            ),
        ),
    )


# 在只读事务中查询待终审凭证，保持审核状态筛选逻辑脱离 HTTP 层。
async def list_pending_final_review_proofs(
    session: AsyncSession,
    season_user_id: int,
) -> tuple[PendingFinalReviewProof, ...]:
    async with session.begin():
        return await fetch_pending_final_review_proofs(
            session,
            season_user_id,
        )


# 校验凭证仍为有效的初审通过状态，禁止重复终审或跨状态直接终审。
def validate_pending_final_review_proof(
    proof: ProofForFinalReview | None,
) -> ProofForFinalReview:
    if proof is None or proof.status != 1:
        raise ProofForFinalReviewNotFoundError
    if proof.review_status != "preliminary_approved":
        raise ProofFinalReviewConflictError
    return proof


# 终审通过时只覆盖结论与评语，不改变已经分配的凭证贡献和项目进度。
async def approve_proof_record(
    session: AsyncSession,
    proof_record_id: int,
    review_comment: str | None,
) -> FinalReviewResult:
    locked_proof = validate_pending_final_review_proof(
        await fetch_proof_for_final_review(
            session,
            proof_record_id,
            for_update=True,
        )
    )
    await update_proof_final_review(
        session,
        proof_record_id,
        "approved",
        review_comment,
    )
    return FinalReviewResult(
        proof_record_id=proof_record_id,
        review_status="approved",
        review_comment=review_comment,
        rolled_back_progress=ZERO_PROGRESS,
        backfilled_progress=ZERO_PROGRESS,
        completion_progress=None,
    )


# 终审拒绝时撤销当前贡献，并按终审通过优先顺序分配释放出的进度空间。
async def reject_proof_record(
    session: AsyncSession,
    proof_snapshot: ProofForFinalReview,
    review_comment: str | None,
) -> FinalReviewResult:
    project_progress = await fetch_locked_project_progress(
        session,
        proof_snapshot.season_user_id,
        proof_snapshot.project_id,
    )
    if project_progress is None:
        raise ProofProgressConsistencyError

    locked_proof = validate_pending_final_review_proof(
        await fetch_proof_for_final_review(
            session,
            proof_snapshot.id,
            for_update=True,
        )
    )
    if (
        locked_proof.season_user_id != proof_snapshot.season_user_id
        or locked_proof.project_id != proof_snapshot.project_id
        or locked_proof.increase > project_progress.completion_progress
    ):
        raise ProofProgressConsistencyError

    released_progress = locked_proof.increase
    completion_progress = (
        project_progress.completion_progress - released_progress
    )
    await update_proof_final_review(
        session,
        locked_proof.id,
        "rejected",
        review_comment,
        ZERO_PROGRESS,
    )

    remaining_progress = released_progress
    backfilled_progress = ZERO_PROGRESS
    candidates = await fetch_locked_backfill_candidates(
        session,
        locked_proof.season_user_id,
        locked_proof.project_id,
        locked_proof.id,
    )
    for candidate in candidates:
        if remaining_progress <= ZERO_PROGRESS:
            break
        available_progress = candidate.progress_delta - candidate.increase
        allocated_progress = min(available_progress, remaining_progress)
        if allocated_progress <= ZERO_PROGRESS:
            continue
        await update_proof_increase(
            session,
            candidate.id,
            candidate.increase + allocated_progress,
        )
        completion_progress += allocated_progress
        backfilled_progress += allocated_progress
        remaining_progress -= allocated_progress

    await update_project_completion_progress(
        session,
        project_progress.id,
        completion_progress,
    )
    return FinalReviewResult(
        proof_record_id=locked_proof.id,
        review_status="rejected",
        review_comment=review_comment,
        rolled_back_progress=released_progress,
        backfilled_progress=backfilled_progress,
        completion_progress=completion_progress,
    )


# 在单一事务中记录终审决定；拒绝分支先锁项目进度，再锁凭证以避免同项目并发重复回补。
async def record_proof_final_review(
    session: AsyncSession,
    proof_record_id: int,
    review_comment: str | None,
    decision: Literal["approved", "rejected"],
) -> FinalReviewResult:
    async with session.begin():
        proof_snapshot = validate_pending_final_review_proof(
            await fetch_proof_for_final_review(
                session,
                proof_record_id,
                for_update=False,
            )
        )
        if decision == "approved":
            return await approve_proof_record(
                session,
                proof_record_id,
                review_comment,
            )
        result = await reject_proof_record(
            session,
            proof_snapshot,
            review_comment,
        )
        notification_context = await fetch_proof_notification_context(
            session,
            proof_record_id,
        )
        if notification_context is None:
            raise ProofProgressConsistencyError
        await insert_notification(
            session,
            notification_context.user_id,
            PROOF_REJECTION_NOTIFICATION_TITLE,
            build_proof_rejection_notification_fields(
                notification_context,
                review_comment,
            ),
        )
        return result
