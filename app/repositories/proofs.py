"""封装管理端凭证查询、终审写入和项目进度调整。"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class PendingFinalReviewProof:
    id: int
    project_id: int
    image_url: str
    created_at: datetime
    proof_date: date
    note: str | None
    preliminary_review_comment: str | None
    review_comment: str | None


@dataclass(frozen=True, slots=True)
class ProofForFinalReview:
    id: int
    season_user_id: int
    project_id: int
    review_status: str
    progress_delta: Decimal
    increase: Decimal
    status: int


@dataclass(frozen=True, slots=True)
class ProofNotificationContext:
    user_id: str
    project_name: str
    proof_date: date


@dataclass(frozen=True, slots=True)
class ProofBackfillCandidate:
    id: int
    review_status: str
    progress_delta: Decimal
    increase: Decimal


@dataclass(frozen=True, slots=True)
class SeasonUserProjectProgress:
    id: int
    completion_progress: Decimal


# 查询指定赛季用户下仍有效且仅完成初审的凭证，并优先返回最近记录。
async def fetch_pending_final_review_proofs(
    session: AsyncSession,
    season_user_id: int,
) -> tuple[PendingFinalReviewProof, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                proof_record.id,
                proof_record.project_id,
                proof_record.image_url,
                proof_record.created_at,
                proof_record.proof_date,
                proof_record.note,
                proof_record.preliminary_review_comment,
                proof_record.review_comment
            FROM proof_record
            WHERE proof_record.season_user_id = :season_user_id
                AND proof_record.review_status = 'preliminary_approved'
                AND proof_record.status = 1
            ORDER BY
                proof_record.proof_date DESC,
                proof_record.created_at DESC,
                proof_record.id DESC
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return tuple(
        PendingFinalReviewProof(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            image_url=str(row["image_url"]),
            created_at=row["created_at"],
            proof_date=row["proof_date"],
            note=str(row["note"]) if row["note"] is not None else None,
            preliminary_review_comment=(
                str(row["preliminary_review_comment"])
                if row["preliminary_review_comment"] is not None
                else None
            ),
            review_comment=(
                str(row["review_comment"])
                if row["review_comment"] is not None
                else None
            ),
        )
        for row in result.mappings().all()
    )


# 按凭证主键读取终审所需状态；写入前可加行锁防止重复或并发终审。
async def fetch_proof_for_final_review(
    session: AsyncSession,
    proof_record_id: int,
    *,
    for_update: bool,
) -> ProofForFinalReview | None:
    lock_clause = "FOR UPDATE" if for_update else ""
    result = await session.exec(
        text(
            f"""
            SELECT
                proof_record.id,
                proof_record.season_user_id,
                proof_record.project_id,
                proof_record.review_status,
                proof_record.progress_delta,
                proof_record.increase,
                proof_record.status
            FROM proof_record
            WHERE proof_record.id = :proof_record_id
            {lock_clause}
            """
        ),
        params={"proof_record_id": proof_record_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return ProofForFinalReview(
        id=int(row["id"]),
        season_user_id=int(row["season_user_id"]),
        project_id=int(row["project_id"]),
        review_status=str(row["review_status"]),
        progress_delta=Decimal(str(row["progress_delta"])),
        increase=Decimal(str(row["increase"])),
        status=int(row["status"]),
    )


# 查询终审拒绝通知所需的用户、项目名称和凭证日期，不扩大终审行锁范围。
async def fetch_proof_notification_context(
    session: AsyncSession,
    proof_record_id: int,
) -> ProofNotificationContext | None:
    result = await session.exec(
        text(
            """
            SELECT
                season_user.user_id,
                project.name AS project_name,
                proof_record.proof_date
            FROM proof_record
            INNER JOIN season_user
                ON season_user.id = proof_record.season_user_id
            INNER JOIN project
                ON project.id = proof_record.project_id
            WHERE proof_record.id = :proof_record_id
            LIMIT 1
            """
        ),
        params={"proof_record_id": proof_record_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return ProofNotificationContext(
        user_id=str(row["user_id"]),
        project_name=str(row["project_name"]),
        proof_date=row["proof_date"],
    )


# 锁定凭证所属的有效用户项目进度，串行化同一项目下的终审回退与回补。
async def fetch_locked_project_progress(
    session: AsyncSession,
    season_user_id: int,
    project_id: int,
) -> SeasonUserProjectProgress | None:
    result = await session.exec(
        text(
            """
            SELECT
                season_user_project.id,
                season_user_project.completion_progress
            FROM season_user_project
            WHERE season_user_project.season_user_id = :season_user_id
                AND season_user_project.project_id = :project_id
                AND season_user_project.status = 1
            FOR UPDATE
            """
        ),
        params={
            "season_user_id": season_user_id,
            "project_id": project_id,
        },
    )
    row = result.mappings().first()
    if row is None:
        return None
    return SeasonUserProjectProgress(
        id=int(row["id"]),
        completion_progress=Decimal(str(row["completion_progress"])),
    )


# 锁定同项目的可回补凭证，终审通过记录优先，其后按上传时间稳定遍历。
async def fetch_locked_backfill_candidates(
    session: AsyncSession,
    season_user_id: int,
    project_id: int,
    excluded_proof_record_id: int,
) -> tuple[ProofBackfillCandidate, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                proof_record.id,
                proof_record.review_status,
                proof_record.progress_delta,
                proof_record.increase
            FROM proof_record
            WHERE proof_record.season_user_id = :season_user_id
                AND proof_record.project_id = :project_id
                AND proof_record.id <> :excluded_proof_record_id
                AND proof_record.status = 1
                AND proof_record.review_status IN (
                    'approved',
                    'preliminary_approved'
                )
                AND proof_record.progress_delta > proof_record.increase
            ORDER BY
                CASE proof_record.review_status
                    WHEN 'approved' THEN 0
                    ELSE 1
                END ASC,
                proof_record.created_at ASC,
                proof_record.id ASC
            FOR UPDATE
            """
        ),
        params={
            "season_user_id": season_user_id,
            "project_id": project_id,
            "excluded_proof_record_id": excluded_proof_record_id,
        },
    )
    return tuple(
        ProofBackfillCandidate(
            id=int(row["id"]),
            review_status=str(row["review_status"]),
            progress_delta=Decimal(str(row["progress_delta"])),
            increase=Decimal(str(row["increase"])),
        )
        for row in result.mappings().all()
    )


# 覆盖凭证终审结论与评语；拒绝时由调用方同时传入归零后的实际贡献。
async def update_proof_final_review(
    session: AsyncSession,
    proof_record_id: int,
    review_status: str,
    review_comment: str | None,
    increase: Decimal | None = None,
) -> None:
    increase_assignment = ", `increase` = :increase" if increase is not None else ""
    params: dict[str, object] = {
        "proof_record_id": proof_record_id,
        "review_status": review_status,
        "review_comment": review_comment,
    }
    if increase is not None:
        params["increase"] = increase
    await session.exec(
        text(
            f"""
            UPDATE proof_record
            SET
                review_status = :review_status,
                review_comment = :review_comment
                {increase_assignment}
            WHERE id = :proof_record_id
            """
        ),
        params=params,
    )


# 更新回补凭证的实际贡献，数值由持有项目行锁的服务层按剩余缺口计算。
async def update_proof_increase(
    session: AsyncSession,
    proof_record_id: int,
    increase: Decimal,
) -> None:
    await session.exec(
        text(
            """
            UPDATE proof_record
            SET `increase` = :increase
            WHERE id = :proof_record_id
            """
        ),
        params={
            "proof_record_id": proof_record_id,
            "increase": increase,
        },
    )


# 写回完成回退与回补后的项目进度，保证其与凭证实际贡献同步提交。
async def update_project_completion_progress(
    session: AsyncSession,
    season_user_project_id: int,
    completion_progress: Decimal,
) -> None:
    await session.exec(
        text(
            """
            UPDATE season_user_project
            SET completion_progress = :completion_progress
            WHERE id = :season_user_project_id
            """
        ),
        params={
            "season_user_project_id": season_user_project_id,
            "completion_progress": completion_progress,
        },
    )
