"""封装赛季结算初始化、资格写入和最终定分所需的数据访问。"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import bindparam, text
from sqlmodel.ext.asyncio.session import AsyncSession


@dataclass(frozen=True, slots=True)
class SettlementSeason:
    id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int


@dataclass(frozen=True, slots=True)
class SettlementUser:
    id: int
    user_id: str
    season_id: int
    season_name: str
    season_start_date: date
    required_project_count: int
    level_reward: int
    final_points: int | None


@dataclass(frozen=True, slots=True)
class SettlementProgress:
    selected_project_count: int
    completed_project_count: int


@dataclass(frozen=True, slots=True)
class SettlementProjectProgress:
    project_id: int
    project_name: str
    completion_progress: Decimal


@dataclass(frozen=True, slots=True)
class SettlementParticipantDetail:
    season_user_id: int
    user_id: str
    username: str
    department_name: str
    avatar_url: str | None
    level_name: str
    projects: tuple[SettlementProjectProgress, ...]
    final_points: int | None
    points_issued: bool


@dataclass(frozen=True, slots=True)
class SettlementPendingFinalReviewProof:
    proof_record_id: int
    season_user_id: int
    project_id: int
    image_url: str
    created_at: datetime
    proof_date: date
    note: str | None
    review_comment: str | None


@dataclass(frozen=True, slots=True)
class SettlementPointIssuanceTarget:
    season_user_id: int
    user_id: str
    season_name: str
    level_name: str
    level_reward: int
    required_project_count: int
    completed_project_count: int
    season_status: int
    final_points: int | None
    points_issued: bool


@dataclass(frozen=True, slots=True)
class SettlementSeasonUserFinalizationTarget:
    season_user_id: int
    user_id: str
    is_formal_participant: bool


@dataclass(frozen=True, slots=True)
class SettlementProjectFinalizationTarget:
    id: int
    project_id: int
    completion_progress: Decimal


@dataclass(frozen=True, slots=True)
class SettlementProofFinalizationTarget:
    id: int
    project_id: int
    review_status: str
    progress_delta: Decimal
    increase: Decimal


# 锁定当前结算中赛季，保证新赛季不会在上一赛季尚未结束时进入结算。
async def lock_settling_seasons(
    session: AsyncSession,
) -> tuple[SettlementSeason, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season.id,
                season.name,
                season.start_date,
                season.end_date,
                season.required_project_count
            FROM season
            WHERE season.status = 2
            ORDER BY season.id
            FOR UPDATE
            """
        )
    )
    return tuple(_map_settlement_season(row) for row in result.mappings())


# 锁定最多两个已到期的进行中赛季，使服务层能够检测违反单赛季约束的数据。
async def lock_expired_active_seasons(
    session: AsyncSession,
    business_date: date,
) -> tuple[SettlementSeason, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season.id,
                season.name,
                season.start_date,
                season.end_date,
                season.required_project_count
            FROM season
            WHERE season.status = 1
              AND season.end_date < :business_date
            ORDER BY season.end_date, season.id
            LIMIT 2
            FOR UPDATE
            """
        ),
        params={"business_date": business_date},
    )
    return tuple(_map_settlement_season(row) for row in result.mappings())


# 将锁定的到期赛季原子推进到结算中，原状态条件用于防御并发重复初始化。
async def mark_season_as_settling(
    session: AsyncSession,
    season_id: int,
) -> bool:
    result = await session.exec(
        text(
            """
            UPDATE season
            SET status = 2
            WHERE id = :season_id
              AND status = 1
            """
        ),
        params={"season_id": season_id},
    )
    return int(result.rowcount) == 1


# 清空上一结算周期的临时补传资格，调用方必须与首次状态流转放在同一事务。
async def clear_supplement_eligibilities(
    session: AsyncSession,
) -> None:
    await session.exec(text("DELETE FROM season_supplement_eligibility"))


# 查询当前结算中赛季；业务约束要求返回结果最多一条。
async def fetch_settling_seasons(
    session: AsyncSession,
) -> tuple[SettlementSeason, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season.id,
                season.name,
                season.start_date,
                season.end_date,
                season.required_project_count
            FROM season
            WHERE season.status = 2
            ORDER BY season.id
            """
        )
    )
    return tuple(_map_settlement_season(row) for row in result.mappings())


# 查询赛季全部参与记录及正式参赛标记；最终复核可加行锁读取最新事实。
async def fetch_season_user_finalization_targets(
    session: AsyncSession,
    season_id: int,
    *,
    for_update: bool = False,
) -> tuple[SettlementSeasonUserFinalizationTarget, ...]:
    lock_clause = "FOR UPDATE" if for_update else ""
    result = await session.exec(
        text(
            f"""
            SELECT
                season_user.id AS season_user_id,
                season_user.user_id,
                CASE
                    WHEN season_user.level_id IS NOT NULL
                     AND season_user.status >= season.required_project_count
                    THEN 1
                    ELSE 0
                END AS is_formal_participant
            FROM season_user
            INNER JOIN season
                ON season.id = season_user.season_id
            WHERE season_user.season_id = :season_id
            ORDER BY season_user.user_id, season_user.id
            {lock_clause}
            """
        ),
        params={"season_id": season_id},
    )
    return tuple(
        SettlementSeasonUserFinalizationTarget(
            season_user_id=int(row["season_user_id"]),
            user_id=str(row["user_id"]),
            is_formal_participant=bool(int(row["is_formal_participant"])),
        )
        for row in result.mappings().all()
    )


# 锁定赛季参与记录并复核归属，非正式参与用户也必须收口其未审核凭证。
async def lock_season_user_for_finalization(
    session: AsyncSession,
    season_user_id: int,
    season_id: int,
) -> bool:
    result = await session.exec(
        text(
            """
            SELECT season_user.id
            FROM season_user
            WHERE season_user.id = :season_user_id
              AND season_user.season_id = :season_id
            FOR UPDATE
            """
        ),
        params={
            "season_user_id": season_user_id,
            "season_id": season_id,
        },
    )
    return result.mappings().first() is not None


# 锁定用户全部有效项目进度，确保一键结算重算期间不能并发终审或回补。
async def lock_settlement_projects_for_finalization(
    session: AsyncSession,
    season_user_id: int,
) -> tuple[SettlementProjectFinalizationTarget, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                season_user_project.id,
                season_user_project.project_id,
                season_user_project.completion_progress
            FROM season_user_project
            WHERE season_user_project.season_user_id = :season_user_id
              AND season_user_project.status = 1
            ORDER BY season_user_project.project_id,
                season_user_project.id
            FOR UPDATE
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return tuple(
        SettlementProjectFinalizationTarget(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            completion_progress=Decimal(str(row["completion_progress"])),
        )
        for row in result.mappings().all()
    )


# 锁定一键结算涉及的有效凭证，终审通过记录参与最终进度重算，其余待审记录被拒绝。
async def lock_settlement_proofs_for_finalization(
    session: AsyncSession,
    season_user_id: int,
) -> tuple[SettlementProofFinalizationTarget, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                proof_record.id,
                proof_record.project_id,
                proof_record.review_status,
                proof_record.progress_delta,
                proof_record.`increase`
            FROM proof_record
            WHERE proof_record.season_user_id = :season_user_id
              AND proof_record.status = 1
              AND proof_record.review_status IN (
                  'pending',
                  'preliminary_approved',
                  'approved'
              )
            ORDER BY proof_record.project_id,
                proof_record.created_at,
                proof_record.id
            FOR UPDATE
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return tuple(
        SettlementProofFinalizationTarget(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            review_status=str(row["review_status"]),
            progress_delta=Decimal(str(row["progress_delta"])),
            increase=Decimal(str(row["increase"])),
        )
        for row in result.mappings().all()
    )


# 分批读取截止时间前仍待初审的正式参赛凭证，避免一次加载无界记录。
async def fetch_pending_initial_review_proof_ids(
    session: AsyncSession,
    season_id: int,
    cutoff_at: datetime,
    limit: int,
) -> tuple[int, ...]:
    result = await session.exec(
        text(
            """
            SELECT proof_record.id
            FROM proof_record
            INNER JOIN season_user
                ON season_user.id = proof_record.season_user_id
            INNER JOIN season
                ON season.id = season_user.season_id
            WHERE season.id = :season_id
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
              AND proof_record.status = 1
              AND proof_record.review_status = 'pending'
              AND proof_record.created_at < :cutoff_at
            ORDER BY proof_record.id
            LIMIT :limit
            """
        ),
        params={
            "season_id": season_id,
            "cutoff_at": cutoff_at,
            "limit": limit,
        },
    )
    return tuple(int(row[0]) for row in result.all())


# 统计截止时间前尚未完成初审的正式参赛凭证，非零时禁止开始用户定分。
async def count_pending_initial_review_proofs(
    session: AsyncSession,
    season_id: int,
    cutoff_at: datetime,
) -> int:
    result = await session.exec(
        text(
            """
            SELECT COUNT(*)
            FROM proof_record
            INNER JOIN season_user
                ON season_user.id = proof_record.season_user_id
            INNER JOIN season
                ON season.id = season_user.season_id
            WHERE season.id = :season_id
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
              AND proof_record.status = 1
              AND proof_record.review_status = 'pending'
              AND proof_record.created_at < :cutoff_at
            """
        ),
        params={"season_id": season_id, "cutoff_at": cutoff_at},
    )
    return int(result.scalar_one())


# 分页读取尚未定分的正式赛季用户，游标避免未完成补传用户阻塞后续记录。
async def fetch_unsettled_season_user_ids(
    session: AsyncSession,
    season_id: int,
    after_id: int,
    limit: int,
) -> tuple[int, ...]:
    result = await session.exec(
        text(
            """
            SELECT season_user.id
            FROM season_user
            INNER JOIN season
                ON season.id = season_user.season_id
            WHERE season_user.season_id = :season_id
              AND season_user.id > :after_id
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
              AND season_user.final_points IS NULL
            ORDER BY season_user.id
            LIMIT :limit
            """
        ),
        params={
            "season_id": season_id,
            "after_id": after_id,
            "limit": limit,
        },
    )
    return tuple(int(row[0]) for row in result.all())


# 查询指定结算赛季的全部正式参赛记录主键，包含已定分和未定分用户。
async def fetch_settlement_season_user_ids(
    session: AsyncSession,
    season_id: int,
) -> tuple[int, ...]:
    result = await session.exec(
        text(
            """
            SELECT season_user.id
            FROM season_user
            INNER JOIN season
                ON season.id = season_user.season_id
            WHERE season.id = :season_id
              AND season.status = 2
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
            ORDER BY season_user.id
            """
        ),
        params={"season_id": season_id},
    )
    return tuple(int(row[0]) for row in result.all())


# 一次查询当前结算赛季全部正式参赛用户的待终审凭证，并优先返回最近记录。
async def fetch_settlement_pending_final_review_proofs(
    session: AsyncSession,
    season_id: int,
) -> tuple[SettlementPendingFinalReviewProof, ...]:
    result = await session.exec(
        text(
            """
            SELECT
                proof_record.id AS proof_record_id,
                proof_record.season_user_id,
                proof_record.project_id,
                proof_record.image_url,
                proof_record.created_at,
                proof_record.proof_date,
                proof_record.note,
                proof_record.review_comment
            FROM proof_record
            INNER JOIN season_user
                ON season_user.id = proof_record.season_user_id
            INNER JOIN season
                ON season.id = season_user.season_id
            WHERE season.id = :season_id
              AND season.status = 2
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
              AND proof_record.review_status = 'preliminary_approved'
              AND proof_record.status = 1
            ORDER BY
                proof_record.proof_date DESC,
                proof_record.created_at DESC,
                proof_record.id DESC
            """
        ),
        params={"season_id": season_id},
    )
    return tuple(
        SettlementPendingFinalReviewProof(
            proof_record_id=int(row["proof_record_id"]),
            season_user_id=int(row["season_user_id"]),
            project_id=int(row["project_id"]),
            image_url=str(row["image_url"]),
            created_at=row["created_at"],
            proof_date=row["proof_date"],
            note=str(row["note"]) if row["note"] is not None else None,
            review_comment=(
                str(row["review_comment"])
                if row["review_comment"] is not None
                else None
            ),
        )
        for row in result.mappings().all()
    )


# 批量查询当前结算赛季参赛详情，并按调用方首次传入的记录主键顺序聚合项目进度。
async def fetch_settlement_participant_details(
    session: AsyncSession,
    season_id: int,
    season_user_ids: tuple[int, ...],
) -> tuple[SettlementParticipantDetail, ...]:
    if not season_user_ids:
        return ()
    statement = text(
        """
        SELECT
            season_user.id AS season_user_id,
            season_user.user_id,
            user_account.name AS username,
            department.name AS department_name,
            user_account.avatar_url,
            project_level.name AS level_name,
            season_user.final_points,
            season_user.points_issued,
            season_user_project.project_id,
            project.name AS project_name,
            season_user_project.completion_progress
        FROM season_user
        INNER JOIN season
            ON season.id = season_user.season_id
            AND season.status = 2
        INNER JOIN `user` AS user_account
            ON user_account.id = season_user.user_id
        INNER JOIN department
            ON department.id = user_account.department_id
        INNER JOIN project_level
            ON project_level.id = season_user.level_id
        LEFT JOIN season_user_project
            ON season_user_project.season_user_id = season_user.id
            AND season_user_project.status = 1
        LEFT JOIN project
            ON project.id = season_user_project.project_id
        WHERE season.id = :season_id
          AND season_user.id IN :season_user_ids
          AND season_user.level_id IS NOT NULL
          AND season_user.status >= season.required_project_count
        ORDER BY season_user.id, season_user_project.id
        """
    ).bindparams(bindparam("season_user_ids", expanding=True))
    result = await session.exec(
        statement,
        params={
            "season_id": season_id,
            "season_user_ids": season_user_ids,
        },
    )
    participant_rows: dict[int, Mapping[str, Any]] = {}
    projects_by_participant: dict[int, list[SettlementProjectProgress]] = {}
    for row in result.mappings().all():
        season_user_id = int(row["season_user_id"])
        participant_rows.setdefault(season_user_id, row)
        projects_by_participant.setdefault(season_user_id, [])
        if row["project_id"] is not None and row["project_name"] is not None:
            projects_by_participant[season_user_id].append(
                SettlementProjectProgress(
                    project_id=int(row["project_id"]),
                    project_name=str(row["project_name"]),
                    completion_progress=Decimal(
                        str(row["completion_progress"])
                    ),
                )
            )
    return tuple(
        SettlementParticipantDetail(
            season_user_id=season_user_id,
            user_id=str(participant_rows[season_user_id]["user_id"]),
            username=str(participant_rows[season_user_id]["username"]),
            department_name=str(
                participant_rows[season_user_id]["department_name"]
            ),
            avatar_url=(
                str(participant_rows[season_user_id]["avatar_url"])
                if participant_rows[season_user_id]["avatar_url"] is not None
                else None
            ),
            level_name=str(participant_rows[season_user_id]["level_name"]),
            projects=tuple(projects_by_participant[season_user_id]),
            final_points=(
                int(participant_rows[season_user_id]["final_points"])
                if participant_rows[season_user_id]["final_points"] is not None
                else None
            ),
            points_issued=bool(
                int(participant_rows[season_user_id]["points_issued"])
            ),
        )
        for season_user_id in season_user_ids
        if season_user_id in participant_rows
    )


# 读取积分发放目标的用户主键，供服务先建立统一用户级积分写入锁顺序。
async def fetch_season_point_issuance_user_id(
    session: AsyncSession,
    season_user_id: int,
) -> str | None:
    result = await session.exec(
        text(
            """
            SELECT season_user.user_id
            FROM season_user
            WHERE season_user.id = :season_user_id
            LIMIT 1
            """
        ),
        params={"season_user_id": season_user_id},
    )
    row = result.mappings().first()
    return str(row["user_id"]) if row is not None else None


# 锁定正式参赛记录并读取定分、发放、赛季和挑战等级快照，防止并发重复发放。
async def lock_season_point_issuance_target(
    session: AsyncSession,
    season_user_id: int,
) -> SettlementPointIssuanceTarget | None:
    result = await session.exec(
        text(
            """
            SELECT
                season_user.id AS season_user_id,
                season_user.user_id,
                season_user.final_points,
                season_user.points_issued,
                season.name AS season_name,
                season.status AS season_status,
                season.required_project_count,
                project_level.name AS level_name,
                project_level.reward AS level_reward,
                (
                    SELECT COUNT(*)
                    FROM season_user_project
                    WHERE season_user_project.season_user_id = season_user.id
                      AND season_user_project.status = 1
                      AND season_user_project.completion_progress >= 1.0000
                ) AS completed_project_count
            FROM season_user
            INNER JOIN season
                ON season.id = season_user.season_id
            INNER JOIN project_level
                ON project_level.id = season_user.level_id
            WHERE season_user.id = :season_user_id
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
            FOR UPDATE
            """
        ),
        params={"season_user_id": season_user_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return SettlementPointIssuanceTarget(
        season_user_id=int(row["season_user_id"]),
        user_id=str(row["user_id"]),
        season_name=str(row["season_name"]),
        level_name=str(row["level_name"]),
        level_reward=int(row["level_reward"]),
        required_project_count=int(row["required_project_count"]),
        completed_project_count=int(row["completed_project_count"]),
        season_status=int(row["season_status"]),
        final_points=(
            int(row["final_points"])
            if row["final_points"] is not None
            else None
        ),
        points_issued=bool(int(row["points_issued"])),
    )


# 新增一条赛季奖励积分流水，并返回数据库生成的流水主键。
async def insert_season_reward_point_record(
    session: AsyncSession,
    user_id: str,
    points: int,
    points_after: int,
    description: str,
) -> int:
    result = await session.exec(
        text(
            """
            INSERT INTO point_record (
                user_id,
                product_id,
                change_type,
                change_points,
                points_after,
                description,
                status,
                gift_distribution_status
            ) VALUES (
                :user_id,
                NULL,
                'season_reward',
                :points,
                :points_after,
                :description,
                1,
                'pending'
            )
            """
        ),
        params={
            "user_id": user_id,
            "points": points,
            "points_after": points_after,
            "description": description,
        },
    )
    return int(result.lastrowid)


# 仅把尚未发放的已定分记录标记为已发放，条件更新用于防御并发和脏状态。
async def mark_season_points_issued(
    session: AsyncSession,
    season_user_id: int,
) -> bool:
    result = await session.exec(
        text(
            """
            UPDATE season_user
            SET points_issued = 1
            WHERE id = :season_user_id
              AND final_points IS NOT NULL
              AND points_issued = 0
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return int(result.rowcount) == 1


# 锁定单个赛季用户并读取等级奖励，串行化定分与终审后的状态收敛。
async def lock_settlement_user(
    session: AsyncSession,
    season_user_id: int,
) -> SettlementUser | None:
    result = await session.exec(
        text(
            """
            SELECT
                season_user.id,
                season_user.user_id,
                season_user.season_id,
                season_user.final_points,
                season.name AS season_name,
                season.start_date AS season_start_date,
                season.required_project_count,
                project_level.reward AS level_reward
            FROM season_user
            INNER JOIN season
                ON season.id = season_user.season_id
            INNER JOIN project_level
                ON project_level.id = season_user.level_id
            WHERE season_user.id = :season_user_id
              AND season.status = 2
              AND season_user.level_id IS NOT NULL
              AND season_user.status >= season.required_project_count
            FOR UPDATE
            """
        ),
        params={"season_user_id": season_user_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return SettlementUser(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        season_id=int(row["season_id"]),
        season_name=str(row["season_name"]),
        season_start_date=row["season_start_date"],
        required_project_count=int(row["required_project_count"]),
        level_reward=int(row["level_reward"]),
        final_points=(
            int(row["final_points"])
            if row["final_points"] is not None
            else None
        ),
    )


# 统计有效选择数和已达成目标数，结算只认完成进度等于百分之百的项目。
async def fetch_settlement_progress(
    session: AsyncSession,
    season_user_id: int,
) -> SettlementProgress:
    result = await session.exec(
        text(
            """
            SELECT
                COUNT(*) AS selected_project_count,
                COALESCE(SUM(
                    CASE
                        WHEN completion_progress = 1.0000 THEN 1
                        ELSE 0
                    END
                ), 0) AS completed_project_count
            FROM season_user_project
            WHERE season_user_id = :season_user_id
              AND status = 1
            """
        ),
        params={"season_user_id": season_user_id},
    )
    row = result.mappings().one()
    return SettlementProgress(
        selected_project_count=int(row["selected_project_count"]),
        completed_project_count=int(row["completed_project_count"]),
    )


# 返回该用户当前全部待终审凭证，具体终审仍按项目行锁优先顺序获取锁。
async def fetch_pending_final_review_proof_ids(
    session: AsyncSession,
    season_user_id: int,
) -> tuple[int, ...]:
    result = await session.exec(
        text(
            """
            SELECT proof_record.id
            FROM proof_record
            WHERE proof_record.season_user_id = :season_user_id
              AND proof_record.status = 1
              AND proof_record.review_status = 'preliminary_approved'
            ORDER BY proof_record.project_id, proof_record.created_at,
                proof_record.id
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return tuple(int(row[0]) for row in result.all())


# 查找已由终审通过原始进度独立撑满项目的额外待终审记录，拒绝后可安全保持满进度。
async def fetch_redundant_pending_final_review_proof_ids(
    session: AsyncSession,
    season_user_id: int,
) -> tuple[int, ...]:
    result = await session.exec(
        text(
            """
            SELECT proof_record.id
            FROM proof_record
            INNER JOIN season_user_project
                ON season_user_project.season_user_id =
                    proof_record.season_user_id
                AND season_user_project.project_id = proof_record.project_id
                AND season_user_project.status = 1
            WHERE proof_record.season_user_id = :season_user_id
              AND proof_record.status = 1
              AND proof_record.review_status = 'preliminary_approved'
              AND season_user_project.completion_progress = 1.0000
              AND (
                  SELECT COALESCE(SUM(approved_proof.progress_delta), 0)
                  FROM proof_record AS approved_proof
                  WHERE approved_proof.season_user_id =
                        proof_record.season_user_id
                    AND approved_proof.project_id = proof_record.project_id
                    AND approved_proof.status = 1
                    AND approved_proof.review_status = 'approved'
              ) >= 1.0000
            ORDER BY proof_record.project_id, proof_record.created_at,
                proof_record.id
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return tuple(int(row[0]) for row in result.all())


# 将待终审凭证逐行登记为补传资格，唯一键保证任务重试不会重复创建。
async def upsert_supplement_eligibilities(
    session: AsyncSession,
    season_user_id: int,
    proof_record_ids: tuple[int, ...],
) -> None:
    if not proof_record_ids:
        return
    await session.exec(
        text(
            """
            INSERT INTO season_supplement_eligibility (
                season_user_id,
                proof_record_id,
                status
            ) VALUES (
                :season_user_id,
                :proof_record_id,
                1
            )
            ON DUPLICATE KEY UPDATE
                season_user_id = VALUES(season_user_id),
                status = 1
            """
        ),
        params=[
            {
                "season_user_id": season_user_id,
                "proof_record_id": proof_record_id,
            }
            for proof_record_id in proof_record_ids
        ],
    )


# 查询用户是否仍有开放补传资格；存在时必须保持 final_points 为空。
async def has_active_supplement_eligibility(
    session: AsyncSession,
    season_user_id: int,
) -> bool:
    result = await session.exec(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM season_supplement_eligibility
                WHERE season_user_id = :season_user_id
                  AND status = 1
            )
            """
        ),
        params={"season_user_id": season_user_id},
    )
    return bool(result.scalar_one())


# 关闭终审通过凭证对应的补传资格，重复关闭保持幂等。
async def close_supplement_eligibility(
    session: AsyncSession,
    proof_record_id: int,
) -> None:
    await session.exec(
        text(
            """
            UPDATE season_supplement_eligibility
            SET status = 0
            WHERE proof_record_id = :proof_record_id
              AND status = 1
            """
        ),
        params={"proof_record_id": proof_record_id},
    )


# 关闭用户所有已经终审通过的资格，补偿终审提交后进程中断造成的状态不同步。
async def close_approved_supplement_eligibilities(
    session: AsyncSession,
    season_user_id: int,
) -> None:
    await session.exec(
        text(
            """
            UPDATE season_supplement_eligibility
            INNER JOIN proof_record
                ON proof_record.id =
                    season_supplement_eligibility.proof_record_id
            SET season_supplement_eligibility.status = 0
            WHERE season_supplement_eligibility.season_user_id =
                    :season_user_id
              AND season_supplement_eligibility.status = 1
              AND proof_record.review_status = 'approved'
            """
        ),
        params={"season_user_id": season_user_id},
    )


# 关闭用户当前全部开放资格，用于已经完整达标而无需继续补传的收口分支。
async def close_user_supplement_eligibilities(
    session: AsyncSession,
    season_user_id: int,
) -> None:
    await session.exec(
        text(
            """
            UPDATE season_supplement_eligibility
            SET status = 0
            WHERE season_user_id = :season_user_id
              AND status = 1
            """
        ),
        params={"season_user_id": season_user_id},
    )


# 清除指定用户尚未进入使用阶段的资格，零完成分支不得留下补传入口。
async def delete_user_supplement_eligibilities(
    session: AsyncSession,
    season_user_id: int,
) -> None:
    await session.exec(
        text(
            """
            DELETE FROM season_supplement_eligibility
            WHERE season_user_id = :season_user_id
            """
        ),
        params={"season_user_id": season_user_id},
    )


# 判断用户是否完整完成指定自然月的已结束赛季，部分完成获得保底分不计入连续完成。
async def did_user_fully_complete_month(
    session: AsyncSession,
    user_id: str,
    month_start: date,
    next_month_start: date,
) -> bool:
    result = await session.exec(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM season
                INNER JOIN season_user
                    ON season_user.season_id = season.id
                WHERE season.status = 3
                  AND season.start_date >= :month_start
                  AND season.start_date < :next_month_start
                  AND season_user.user_id = :user_id
                  AND season_user.final_points IS NOT NULL
                  AND season_user.level_id IS NOT NULL
                  AND season_user.status >= season.required_project_count
                  AND (
                      SELECT COUNT(*)
                      FROM season_user_project
                      WHERE season_user_project.season_user_id = season_user.id
                        AND season_user_project.status = 1
                  ) = season.required_project_count
                  AND (
                      SELECT COUNT(*)
                      FROM season_user_project
                      WHERE season_user_project.season_user_id = season_user.id
                        AND season_user_project.status = 1
                        AND season_user_project.completion_progress = 1.0000
                  ) = season.required_project_count
            )
            """
        ),
        params={
            "user_id": user_id,
            "month_start": month_start,
            "next_month_start": next_month_start,
        },
    )
    return bool(result.scalar_one())


# 仅在尚未定分时写入最终积分，行锁与空值条件共同保证通知和定分只发生一次。
async def set_final_points(
    session: AsyncSession,
    season_user_id: int,
    final_points: int,
) -> bool:
    result = await session.exec(
        text(
            """
            UPDATE season_user
            SET final_points = :final_points
            WHERE id = :season_user_id
              AND final_points IS NULL
            """
        ),
        params={
            "season_user_id": season_user_id,
            "final_points": final_points,
        },
    )
    return int(result.rowcount) == 1


# 在所有正式参与用户完成定分和积分发放且无开放资格后结束赛季。
async def mark_season_ended_if_complete(
    session: AsyncSession,
    season_id: int,
) -> bool:
    result = await session.exec(
        text(
            """
            UPDATE season
            SET status = 3
            WHERE id = :season_id
              AND status = 2
              AND NOT EXISTS (
                  SELECT 1
                  FROM season_user
                  WHERE season_user.season_id = season.id
                    AND season_user.level_id IS NOT NULL
                    AND season_user.status >= season.required_project_count
                    AND (
                        season_user.final_points IS NULL
                        OR season_user.points_issued = 0
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM season_supplement_eligibility
                  INNER JOIN season_user
                      ON season_user.id =
                          season_supplement_eligibility.season_user_id
                  WHERE season_user.season_id = season.id
                    AND season_supplement_eligibility.status = 1
              )
            """
        ),
        params={"season_id": season_id},
    )
    return int(result.rowcount) == 1


# 将数据库映射统一转换为不可变赛季对象，避免各查询重复处理类型。
def _map_settlement_season(
    mapping: Mapping[str, Any],
) -> SettlementSeason:
    return SettlementSeason(
        id=int(mapping["id"]),
        name=str(mapping["name"]),
        start_date=mapping["start_date"],
        end_date=mapping["end_date"],
        required_project_count=int(mapping["required_project_count"]),
    )
