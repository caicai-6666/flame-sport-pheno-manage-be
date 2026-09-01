"""编排赛季结算初始化、遗留初审、补传资格、定分和积分发放。"""

import asyncio
import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from app.clients.client_backend import ClientBackendClient
from app.core.config import get_settings
from app.repositories.notifications import (
    NotificationField,
    insert_notification,
)
from app.repositories.proofs import (
    fetch_proof_for_final_review,
    update_project_completion_progress,
    update_proof_final_review,
    update_proof_increase,
)
from app.repositories.products import (
    fetch_latest_point_record_for_update,
    lock_user_for_point_update,
)
from app.repositories.season_settlements import (
    SettlementParticipantDetail,
    SettlementPendingFinalReviewProof,
    SettlementPointIssuanceTarget,
    SettlementProjectFinalizationTarget,
    SettlementProofFinalizationTarget,
    SettlementSeason,
    SettlementSeasonUserFinalizationTarget,
    SettlementUser,
    clear_supplement_eligibilities,
    close_approved_supplement_eligibilities,
    close_supplement_eligibility,
    close_user_supplement_eligibilities,
    count_pending_initial_review_proofs,
    count_pending_supplement_review_proofs,
    delete_user_supplement_eligibilities,
    did_user_fully_complete_month,
    fetch_pending_final_review_proof_ids,
    fetch_pending_initial_review_proof_ids,
    fetch_pending_supplement_review_proof_ids,
    fetch_redundant_pending_final_review_proof_ids,
    fetch_season_point_issuance_user_id,
    fetch_season_user_finalization_targets,
    fetch_settlement_participant_details,
    fetch_settlement_pending_final_review_proofs,
    fetch_settlement_progress,
    fetch_settlement_season_user_ids,
    fetch_settling_seasons,
    fetch_unsettled_season_user_ids,
    has_active_supplement_eligibility,
    insert_season_reward_point_record,
    lock_expired_active_seasons,
    lock_open_seasons,
    lock_season_point_issuance_target,
    lock_season_user_for_finalization,
    lock_settlement_projects_for_finalization,
    lock_settlement_proofs_for_finalization,
    lock_settlement_user,
    lock_settling_seasons,
    mark_season_as_settling,
    mark_season_as_active,
    mark_season_ended_if_complete,
    mark_season_points_issued,
    set_final_points,
    upsert_supplement_eligibilities,
)
from app.services.proofs import (
    FinalReviewResult,
    reject_proof_record,
    validate_pending_final_review_proof,
)

logger = logging.getLogger(__name__)

PARTIAL_COMPLETION_POINTS = 20
MAX_UNSIGNED_INT = 4_294_967_295
SETTLEMENT_NOTIFICATION_TITLE = "赛季结算结果"
REDUNDANT_PROOF_NOTIFICATION_TITLE = "赛季凭证处理提示"
ZERO_COMPLETION_REJECTION_COMMENT = (
    "赛季结算时尚未达成任何项目目标，本条记录不再进入终审。"
)
REDUNDANT_PROOF_REJECTION_COMMENT = (
    "该项目已由终审通过记录达成目标，本条额外记录无需继续终审。"
)
ONE_CLICK_PROOF_REJECTION_COMMENT = (
    "赛季一键结算时仍未完成终审，本条记录按赛季收口规则拒绝。"
)
FULL_PROGRESS = Decimal("1.0000")
ZERO_PROGRESS = Decimal("0.0000")


class MultipleSettlingSeasonsError(RuntimeError):
    """数据库违反最多一个结算中赛季的业务约束。"""


class SettlementProgressConsistencyError(RuntimeError):
    """赛季用户的有效项目数量与赛季要求不一致，不能继续定分。"""


class SettlementTransitionConflictError(RuntimeError):
    """锁定到期赛季后状态更新失败，结算初始化必须整体回滚。"""


class SeasonActivationConsistencyError(RuntimeError):
    """赛季数据违反自动激活所依赖的唯一进行中或唯一到期候选约束。"""


class SettlingSeasonNotFoundError(RuntimeError):
    """当前没有处于结算中的赛季。"""


class SettlementParticipantNotFoundError(RuntimeError):
    """指定赛季参赛记录不存在或不属于正式参赛用户。"""


class SeasonPointsNotFinalizedError(RuntimeError):
    """指定赛季参赛记录尚未完成最终定分。"""


class SeasonPointIssuanceNotAllowedError(RuntimeError):
    """未发放记录所属赛季当前不处于结算状态。"""


class SeasonPointIssuanceConsistencyError(RuntimeError):
    """积分余额、用户归属或发放状态不一致，必须回滚。"""


class SeasonPointBalanceOverflowError(RuntimeError):
    """发放后的用户积分余额超过数据库无符号整数上限。"""


class SeasonCompletionConflictError(RuntimeError):
    """一键结算锁定期间赛季或参与记录发生变化，必须整体回滚。"""


class SeasonCompletionConsistencyError(RuntimeError):
    """一键结算无法满足最终状态约束，禁止把赛季标记为已结束。"""


@dataclass(frozen=True, slots=True)
class PointsBreakdown:
    completed_project_count: int
    required_project_count: int
    base_points: int
    streak_bonus: int

    # 返回基础积分与连续奖励之和，不触碰用户全局积分余额。
    @property
    def final_points(self) -> int:
        return self.base_points + self.streak_bonus


@dataclass(frozen=True, slots=True)
class SettlementCycleResult:
    transitioned_season_id: int | None
    settling_season_id: int | None
    pending_initial_review_count: int
    finalized_user_count: int
    created_eligibility_count: int
    season_ended: bool


@dataclass(frozen=True, slots=True)
class UserSettlementResult:
    finalized: bool = False
    created_eligibility_count: int = 0


@dataclass(frozen=True, slots=True)
class SettlementFinalReviewPolicy:
    applies: bool
    force_rejection: bool
    already_finalized: bool = False
    automatic_result: FinalReviewResult | None = None


@dataclass(frozen=True, slots=True)
class SettlingSeasonOverview:
    season_id: int
    name: str
    start_date: date
    end_date: date
    required_project_count: int
    status: int
    season_user_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SeasonPointIssuanceResult:
    season_user_id: int
    final_points: int
    points_issued: bool
    issued_now: bool


@dataclass(frozen=True, slots=True)
class SeasonCompletionResult:
    season_id: int
    participant_count: int
    rejected_proof_count: int
    finalized_user_count: int
    issued_user_count: int
    season_ended: bool


@dataclass(frozen=True, slots=True)
class SeasonUserCompletionResult:
    rejected_proof_count: int
    finalized: bool
    issued_now: bool


# 把给定日期归一到自然月首日，连续完成奖励只按自然月判断。
def month_start(value: date) -> date:
    return value.replace(day=1)


# 返回目标月份前若干个月的首日，不依赖非标准日期库。
def subtract_months(value: date, months: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    return date(year, zero_based_month + 1, 1)


# 返回下一自然月首日，用于构造无时区歧义的左闭右开月份范围。
def next_month_start(value: date) -> date:
    days_in_month = monthrange(value.year, value.month)[1]
    return value.replace(day=days_in_month) + timedelta(days=1)


# 根据完成项目数计算基础积分，并阻止可配置奖励使最终积分越过数据库范围。
def calculate_points_breakdown(
    completed_project_count: int,
    required_project_count: int,
    level_reward: int,
    streak_bonus: int,
) -> PointsBreakdown:
    if (
        not 0 <= completed_project_count <= required_project_count
        or level_reward < 0
        or streak_bonus < 0
    ):
        raise SettlementProgressConsistencyError
    if completed_project_count == 0:
        base_points = 0
    elif completed_project_count < required_project_count:
        base_points = PARTIAL_COMPLETION_POINTS
    else:
        base_points = level_reward
    effective_streak_bonus = (
        streak_bonus
        if completed_project_count == required_project_count
        else 0
    )
    if base_points + effective_streak_bonus > MAX_UNSIGNED_INT:
        raise SettlementProgressConsistencyError
    return PointsBreakdown(
        completed_project_count=completed_project_count,
        required_project_count=required_project_count,
        base_points=base_points,
        streak_bonus=effective_streak_bonus,
    )


# 在同一事务中推进唯一到期赛季并清空临时资格，重复轮询不会再次清空。
async def initialize_expired_season(
    session: AsyncSession,
    business_date: date,
) -> SettlementSeason | None:
    async with session.begin():
        settling_seasons = await lock_settling_seasons(session)
        if len(settling_seasons) > 1:
            raise MultipleSettlingSeasonsError
        if settling_seasons:
            return None

        expired_seasons = await lock_expired_active_seasons(
            session,
            business_date,
        )
        if len(expired_seasons) > 1:
            raise MultipleSettlingSeasonsError(
                "同时存在多个已到期的进行中赛季"
            )
        if not expired_seasons:
            return None

        expired_season = expired_seasons[0]
        if not await mark_season_as_settling(session, expired_season.id):
            raise SettlementTransitionConflictError
        await clear_supplement_eligibilities(session)
        return expired_season


# 按上海业务日期激活唯一已到开始日的赛季，结算中赛季不阻塞新赛季开放。
async def activate_due_season(
    session: AsyncSession,
    business_date: date,
) -> SettlementSeason | None:
    async with session.begin():
        open_seasons = await lock_open_seasons(session)
        active_seasons = tuple(
            season for season, status in open_seasons if status == 1
        )
        if len(active_seasons) > 1:
            raise SeasonActivationConsistencyError(
                "同时存在多个进行中赛季"
            )
        if active_seasons:
            return None

        due_seasons = tuple(
            season
            for season, status in open_seasons
            if status == 0
            and season.start_date <= business_date <= season.end_date
        )
        if len(due_seasons) > 1:
            raise SeasonActivationConsistencyError(
                "同时存在多个应激活的未开始赛季"
            )
        if not due_seasons:
            return None

        due_season = due_seasons[0]
        if not await mark_season_as_active(session, due_season.id):
            raise SeasonActivationConsistencyError(
                "目标赛季状态已发生并发变化"
            )
        return due_season


# 在短只读事务中获取当前唯一结算赛季，发现脏状态时拒绝继续写业务数据。
async def get_single_settling_season(
    session: AsyncSession,
) -> SettlementSeason | None:
    async with session.begin():
        seasons = await fetch_settling_seasons(session)
    if len(seasons) > 1:
        raise MultipleSettlingSeasonsError
    return seasons[0] if seasons else None


# 在只读事务中返回唯一结算赛季及正式参赛记录列表，异常数据不得静默选取。
async def get_settling_season_overview(
    session: AsyncSession,
) -> SettlingSeasonOverview:
    async with session.begin():
        seasons = await fetch_settling_seasons(session)
        if len(seasons) > 1:
            raise MultipleSettlingSeasonsError
        if not seasons:
            raise SettlingSeasonNotFoundError
        season = seasons[0]
        season_user_ids = await fetch_settlement_season_user_ids(
            session,
            season.id,
        )
    return SettlingSeasonOverview(
        season_id=season.id,
        name=season.name,
        start_date=season.start_date,
        end_date=season.end_date,
        required_project_count=season.required_project_count,
        status=2,
        season_user_ids=season_user_ids,
    )


# 去除重复赛季用户主键并保留首次出现位置，避免重复查询和重复响应。
def deduplicate_season_user_ids(
    season_user_ids: list[int],
) -> tuple[int, ...]:
    return tuple(dict.fromkeys(season_user_ids))


# 在唯一结算赛季范围内批量查询正式参赛详情，其他赛季或非正式记录不返回。
async def get_settlement_participant_details(
    session: AsyncSession,
    season_user_ids: list[int],
) -> tuple[SettlementParticipantDetail, ...]:
    unique_season_user_ids = deduplicate_season_user_ids(season_user_ids)
    async with session.begin():
        seasons = await fetch_settling_seasons(session)
        if len(seasons) > 1:
            raise MultipleSettlingSeasonsError
        if not seasons:
            raise SettlingSeasonNotFoundError
        return await fetch_settlement_participant_details(
            session,
            seasons[0].id,
            unique_season_user_ids,
        )


# 在唯一结算中赛季范围内返回全部正式参赛用户的待终审凭证。
async def get_settlement_pending_final_review_proofs(
    session: AsyncSession,
) -> tuple[SettlementPendingFinalReviewProof, ...]:
    async with session.begin():
        seasons = await fetch_settling_seasons(session)
        if len(seasons) > 1:
            raise MultipleSettlingSeasonsError
        if not seasons:
            raise SettlingSeasonNotFoundError
        return await fetch_settlement_pending_final_review_proofs(
            session,
            seasons[0].id,
        )


# 根据项目完成档位拆分保底、挑战和连续奖励积分，并兼容配置调整前已定分的结果。
def build_season_reward_description(
    target: SettlementPointIssuanceTarget,
) -> str:
    if target.final_points is None:
        raise SeasonPointsNotFinalizedError
    if (
        target.required_project_count <= 0
        or target.completed_project_count < 0
        or target.completed_project_count > target.required_project_count
        or target.level_reward < 0
    ):
        raise SeasonPointIssuanceConsistencyError
    if target.completed_project_count == 0:
        if target.final_points != 0:
            raise SeasonPointIssuanceConsistencyError
        return (
            f"{target.season_name}{target.level_name}结算完成："
            "本赛季暂未达成项目目标，本次积分为0分。"
            "感谢您的参与，坚持运动就是进步，下个赛季继续加油！"
        )
    if target.completed_project_count < target.required_project_count:
        if target.final_points != PARTIAL_COMPLETION_POINTS:
            raise SeasonPointIssuanceConsistencyError
        return (
            f"恭喜您在{target.season_name}达成"
            f"{target.completed_project_count}/"
            f"{target.required_project_count}个项目目标，"
            f"获得{target.final_points}分保底积分！"
            "感谢您的坚持，下个赛季继续向全部达成冲刺！"
        )

    streak_bonus = target.final_points - target.level_reward
    if streak_bonus < 0:
        raise SeasonPointIssuanceConsistencyError
    if streak_bonus == 0:
        return (
            f"恭喜您达成{target.season_name}{target.level_name}，"
            f"获得{target.level_reward}分挑战积分"
        )
    return (
        f"恭喜您达成{target.season_name}{target.level_name}，"
        f"获得{target.level_reward}分挑战积分、"
        f"{streak_bonus}分连续完成额外奖励积分，"
        f"合计{target.final_points}分"
    )


# 在调用方事务内串行化用户积分写入，已发放记录直接幂等返回。
async def issue_season_points_in_transaction(
    session: AsyncSession,
    season_user_id: int,
) -> SeasonPointIssuanceResult:
    user_id = await fetch_season_point_issuance_user_id(
        session,
        season_user_id,
    )
    if user_id is None:
        raise SettlementParticipantNotFoundError
    if not await lock_user_for_point_update(session, user_id):
        raise SeasonPointIssuanceConsistencyError
    target = await lock_season_point_issuance_target(
        session,
        season_user_id,
    )
    if target is None:
        raise SettlementParticipantNotFoundError
    if target.user_id != user_id:
        raise SeasonPointIssuanceConsistencyError
    if target.points_issued:
        if target.final_points is None:
            raise SeasonPointIssuanceConsistencyError
        return SeasonPointIssuanceResult(
            season_user_id=target.season_user_id,
            final_points=target.final_points,
            points_issued=True,
            issued_now=False,
        )
    if target.season_status != 2:
        raise SeasonPointIssuanceNotAllowedError
    if target.final_points is None:
        raise SeasonPointsNotFinalizedError
    description = build_season_reward_description(target)

    latest_point_record = await fetch_latest_point_record_for_update(
        session,
        target.user_id,
    )
    current_points = (
        latest_point_record.points_after
        if latest_point_record is not None
        else 0
    )
    points_after = current_points + target.final_points
    if points_after > MAX_UNSIGNED_INT:
        raise SeasonPointBalanceOverflowError
    await insert_season_reward_point_record(
        session,
        target.user_id,
        target.final_points,
        points_after,
        description,
    )
    if not await mark_season_points_issued(
        session,
        target.season_user_id,
    ):
        raise SeasonPointIssuanceConsistencyError
    return SeasonPointIssuanceResult(
        season_user_id=target.season_user_id,
        final_points=target.final_points,
        points_issued=True,
        issued_now=True,
    )


# 在单一事务内发放一个用户的赛季积分，供独立管理接口安全调用。
async def issue_season_points(
    session: AsyncSession,
    season_user_id: int,
) -> SeasonPointIssuanceResult:
    async with session.begin():
        return await issue_season_points_in_transaction(
            session,
            season_user_id,
        )


# 调用一条凭证的立即初审；网络和契约错误留待下轮重试，不伪造拒绝结果。
async def review_pending_proof(
    client_backend: ClientBackendClient,
    proof_record_id: int,
    semaphore: asyncio.Semaphore,
) -> bool:
    try:
        async with semaphore:
            await client_backend.review_proof_immediately(proof_record_id)
        return True
    except httpx.HTTPStatusError as error:
        logger.warning(
            "结算遗留凭证立即初审未完成 proof_record_id=%s status=%s",
            proof_record_id,
            error.response.status_code,
        )
    except (httpx.RequestError, ValueError):
        logger.warning(
            "结算遗留凭证立即初审调用失败 proof_record_id=%s",
            proof_record_id,
            exc_info=True,
        )
    return False


# 调用补交专用初审；该入口由客户后端强制读取资格表固化的审核上下文。
async def review_pending_supplement(
    client_backend: ClientBackendClient,
    proof_record_id: int,
    semaphore: asyncio.Semaphore,
) -> bool:
    try:
        async with semaphore:
            await client_backend.review_supplement_immediately(proof_record_id)
        return True
    except httpx.HTTPStatusError as error:
        logger.warning(
            "结算补交凭证初审未完成 proof_record_id=%s status=%s",
            proof_record_id,
            error.response.status_code,
        )
    except (httpx.RequestError, ValueError):
        logger.warning(
            "结算补交凭证初审调用失败 proof_record_id=%s",
            proof_record_id,
            exc_info=True,
        )
    return False


# 分批并发清理赛季截止前遗留初审；任一失败时停止本轮，避免对永久失败记录忙重试。
async def drain_pending_initial_reviews(
    session_factory: async_sessionmaker[AsyncSession],
    client_backend: ClientBackendClient,
    season: SettlementSeason,
    batch_size: int,
    concurrency: int,
) -> int:
    cutoff_at = datetime.combine(
        season.end_date + timedelta(days=1),
        time.min,
    )
    semaphore = asyncio.Semaphore(concurrency)
    while True:
        async with session_factory() as session:
            async with session.begin():
                proof_record_ids = (
                    await fetch_pending_initial_review_proof_ids(
                        session,
                        season.id,
                        cutoff_at,
                        batch_size,
                    )
                )
        if not proof_record_ids:
            return 0

        results = await asyncio.gather(
            *(
                review_pending_proof(
                    client_backend,
                    proof_record_id,
                    semaphore,
                )
                for proof_record_id in proof_record_ids
            )
        )
        if not all(results):
            break

    async with session_factory() as session:
        async with session.begin():
            return await count_pending_initial_review_proofs(
                session,
                season.id,
                cutoff_at,
            )


# 分批初审已补交记录，扫描依据资格状态而不是原赛季上传截止时间。
async def drain_pending_supplement_reviews(
    session_factory: async_sessionmaker[AsyncSession],
    client_backend: ClientBackendClient,
    season: SettlementSeason,
    batch_size: int,
    concurrency: int,
) -> int:
    semaphore = asyncio.Semaphore(concurrency)
    while True:
        async with session_factory() as session:
            async with session.begin():
                proof_record_ids = (
                    await fetch_pending_supplement_review_proof_ids(
                        session,
                        season.id,
                        batch_size,
                    )
                )
        if not proof_record_ids:
            return 0
        results = await asyncio.gather(
            *(
                review_pending_supplement(
                    client_backend,
                    proof_record_id,
                    semaphore,
                )
                for proof_record_id in proof_record_ids
            )
        )
        if not all(results):
            break

    async with session_factory() as session:
        async with session.begin():
            return await count_pending_supplement_review_proofs(
                session,
                season.id,
            )


# 按当前环境配置计算两个月或三个月连续奖励，三个月档替代两个月档。
async def calculate_streak_bonus(
    session: AsyncSession,
    user_id: str,
    current_season_start: date,
) -> int:
    settings = get_settings()
    current_month = month_start(current_season_start)
    previous_month = subtract_months(current_month, 1)
    if not await did_user_fully_complete_month(
        session,
        user_id,
        previous_month,
        next_month_start(previous_month),
    ):
        return 0

    two_months_ago = subtract_months(current_month, 2)
    if await did_user_fully_complete_month(
        session,
        user_id,
        two_months_ago,
        next_month_start(two_months_ago),
    ):
        return settings.season_settlement_three_month_streak_bonus_points
    return settings.season_settlement_two_month_streak_bonus_points


# 构造定分通知字段，零完成时明确说明不开放补传，其余分支展示积分构成。
def build_settlement_notification_fields(
    user: SettlementUser,
    breakdown: PointsBreakdown,
) -> tuple[NotificationField, ...]:
    fields = [
        NotificationField("赛季", user.season_name),
        NotificationField(
            "完成项目",
            (
                f"{breakdown.completed_project_count}/"
                f"{breakdown.required_project_count}"
            ),
        ),
        NotificationField("最终积分", str(breakdown.final_points)),
    ]
    if breakdown.completed_project_count == 0:
        fields.append(
            NotificationField(
                "结算说明",
                "未达成任何项目目标，不开放赛后补传。",
            )
        )
    elif breakdown.streak_bonus > 0:
        fields.extend(
            (
                NotificationField("挑战积分", str(breakdown.base_points)),
                NotificationField(
                    "连续完成奖励",
                    str(breakdown.streak_bonus),
                ),
            )
        )
    return tuple(fields)


# 在持有用户行锁的事务内批量拒绝零完成用户的待终审凭证，不产生逐条拒绝通知。
async def reject_zero_completion_proofs(
    session: AsyncSession,
    season_user_id: int,
    proof_record_ids: tuple[int, ...],
) -> None:
    await delete_user_supplement_eligibilities(session, season_user_id)
    for proof_record_id in proof_record_ids:
        proof_snapshot = validate_pending_final_review_proof(
            await fetch_proof_for_final_review(
                session,
                proof_record_id,
                for_update=False,
            )
        )
        await reject_proof_record(
            session,
            proof_snapshot,
            ZERO_COMPLETION_REJECTION_COMMENT,
        )


# 拒绝不会改变最终项目达成结果的额外记录，并为同一批自动处理只创建一条提示。
async def reject_redundant_final_review_proofs(
    session: AsyncSession,
    user: SettlementUser,
    proof_record_ids: tuple[int, ...],
) -> dict[int, FinalReviewResult]:
    results: dict[int, FinalReviewResult] = {}
    for proof_record_id in proof_record_ids:
        proof_snapshot = validate_pending_final_review_proof(
            await fetch_proof_for_final_review(
                session,
                proof_record_id,
                for_update=False,
            )
        )
        results[proof_record_id] = await reject_proof_record(
            session,
            proof_snapshot,
            REDUNDANT_PROOF_REJECTION_COMMENT,
        )
        await close_supplement_eligibility(
            session,
            proof_record_id,
        )
    if results:
        await insert_notification(
            session,
            user.user_id,
            REDUNDANT_PROOF_NOTIFICATION_TITLE,
            (
                NotificationField("赛季", user.season_name),
                NotificationField("处理结果", "额外凭证已关闭"),
                NotificationField("关闭记录", str(len(results))),
                NotificationField(
                    "处理说明",
                    "相关项目已由终审通过记录达成目标，无需继续终审。",
                ),
            ),
        )
    return results


# 自动收口由终审通过记录独立撑满项目的额外待终审凭证，并返回实际拒绝结果。
async def prune_redundant_final_reviews(
    session: AsyncSession,
    user: SettlementUser,
) -> dict[int, FinalReviewResult]:
    proof_record_ids = (
        await fetch_redundant_pending_final_review_proof_ids(
            session,
            user.id,
        )
    )
    return await reject_redundant_final_review_proofs(
        session,
        user,
        proof_record_ids,
    )


# 在终审写入前锁定结算用户；合格用户补齐资格，零完成用户强制走结算拒绝规则。
async def prepare_settling_user_for_final_review(
    session: AsyncSession,
    season_user_id: int,
    proof_record_id: int,
) -> SettlementFinalReviewPolicy:
    user = await lock_settlement_user(session, season_user_id)
    if user is None:
        return SettlementFinalReviewPolicy(
            applies=False,
            force_rejection=False,
        )
    if user.final_points is not None:
        return SettlementFinalReviewPolicy(
            applies=True,
            force_rejection=False,
            already_finalized=True,
        )
    automatic_results = await prune_redundant_final_reviews(session, user)
    if proof_record_id in automatic_results:
        return SettlementFinalReviewPolicy(
            applies=True,
            force_rejection=False,
            automatic_result=automatic_results[proof_record_id],
        )
    progress = await fetch_settlement_progress(session, user.id)
    if progress.selected_project_count != user.required_project_count:
        raise SettlementProgressConsistencyError(
            f"season_user_id={user.id} 有效项目数量不一致"
        )
    pending_final_review_ids = await fetch_pending_final_review_proof_ids(
        session,
        user.id,
    )
    if await has_active_supplement_eligibility(session, user.id):
        await upsert_supplement_eligibilities(
            session,
            user.id,
            pending_final_review_ids,
        )
        return SettlementFinalReviewPolicy(
            applies=True,
            force_rejection=False,
        )
    if progress.completed_project_count == 0:
        return SettlementFinalReviewPolicy(
            applies=True,
            force_rejection=True,
        )
    await upsert_supplement_eligibilities(
        session,
        user.id,
        pending_final_review_ids,
    )
    return SettlementFinalReviewPolicy(
        applies=True,
        force_rejection=False,
    )


# 原子写入最终积分和汇总通知，final_points 空值条件保证任务重试不会重复通知。
async def finalize_settlement_user(
    session: AsyncSession,
    user: SettlementUser,
    completed_project_count: int,
) -> bool:
    streak_bonus = 0
    if completed_project_count == user.required_project_count:
        streak_bonus = await calculate_streak_bonus(
            session,
            user.user_id,
            user.season_start_date,
        )
    breakdown = calculate_points_breakdown(
        completed_project_count,
        user.required_project_count,
        user.level_reward,
        streak_bonus,
    )
    if not await set_final_points(
        session,
        user.id,
        breakdown.final_points,
    ):
        return False
    await insert_notification(
        session,
        user.user_id,
        SETTLEMENT_NOTIFICATION_TITLE,
        build_settlement_notification_fields(user, breakdown),
    )
    return True


# 仅按终审通过凭证重新分配最终进度，保证待审凭证关闭后项目与凭证贡献一致。
def calculate_final_approved_progress(
    projects: tuple[SettlementProjectFinalizationTarget, ...],
    proofs: tuple[SettlementProofFinalizationTarget, ...],
) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    project_ids = [project.project_id for project in projects]
    if len(project_ids) != len(set(project_ids)):
        raise SettlementProgressConsistencyError(
            "赛季用户存在重复的有效项目"
        )

    remaining_by_project = {
        project.project_id: FULL_PROGRESS for project in projects
    }
    progress_by_project = {
        project.project_id: ZERO_PROGRESS for project in projects
    }
    increase_by_proof: dict[int, Decimal] = {}
    for proof in proofs:
        if proof.review_status != "approved":
            continue
        if (
            proof.progress_delta < ZERO_PROGRESS
            or proof.progress_delta > FULL_PROGRESS
        ):
            raise SettlementProgressConsistencyError(
                f"proof_record_id={proof.id} 原始贡献越界"
            )
        remaining = remaining_by_project.get(proof.project_id)
        if remaining is None:
            continue
        allocated = min(proof.progress_delta, remaining)
        increase_by_proof[proof.id] = allocated
        progress_by_project[proof.project_id] += allocated
        remaining_by_project[proof.project_id] -= allocated
    return increase_by_proof, progress_by_project


# 拒绝用户全部待初审和待终审凭证，并把项目进度重建为终审通过凭证贡献之和。
async def reject_unreviewed_proofs_and_rebuild_progress(
    session: AsyncSession,
    season_user_id: int,
) -> int:
    projects = await lock_settlement_projects_for_finalization(
        session,
        season_user_id,
    )
    proofs = await lock_settlement_proofs_for_finalization(
        session,
        season_user_id,
    )
    increase_by_proof, progress_by_project = (
        calculate_final_approved_progress(projects, proofs)
    )

    rejected_count = 0
    for proof in proofs:
        if proof.review_status in {"pending", "preliminary_approved"}:
            await update_proof_final_review(
                session,
                proof.id,
                "rejected",
                ONE_CLICK_PROOF_REJECTION_COMMENT,
                ZERO_PROGRESS,
            )
            rejected_count += 1
            continue
        final_increase = increase_by_proof.get(proof.id)
        if final_increase is not None and final_increase != proof.increase:
            await update_proof_increase(
                session,
                proof.id,
                final_increase,
            )

    for project in projects:
        final_progress = progress_by_project[project.project_id]
        if final_progress != project.completion_progress:
            await update_project_completion_progress(
                session,
                project.id,
                final_progress,
            )
    return rejected_count


# 为一键结算自动关闭的凭证写入单条汇总通知，避免逐记录制造消息噪音。
async def notify_one_click_proof_rejections(
    session: AsyncSession,
    target: SettlementSeasonUserFinalizationTarget,
    season: SettlementSeason,
    rejected_proof_count: int,
) -> None:
    if rejected_proof_count <= 0:
        return
    await insert_notification(
        session,
        target.user_id,
        REDUNDANT_PROOF_NOTIFICATION_TITLE,
        (
            NotificationField("赛季", season.name),
            NotificationField("处理结果", "未完成审核凭证已拒绝"),
            NotificationField("关闭记录", str(rejected_proof_count)),
            NotificationField(
                "处理说明",
                "赛季已执行最终结算，相关凭证不再进入后续审核。",
            ),
        ),
    )


# 在一键结算事务内收口单个赛季用户；非正式用户只处理凭证，资格由赛季级清空。
async def complete_season_user_in_transaction(
    session: AsyncSession,
    season: SettlementSeason,
    target: SettlementSeasonUserFinalizationTarget,
) -> SeasonUserCompletionResult:
    if not await lock_season_user_for_finalization(
        session,
        target.season_user_id,
        season.id,
    ):
        raise SeasonCompletionConflictError

    rejected_proof_count = (
        await reject_unreviewed_proofs_and_rebuild_progress(
            session,
            target.season_user_id,
        )
    )
    await notify_one_click_proof_rejections(
        session,
        target,
        season,
        rejected_proof_count,
    )
    if not target.is_formal_participant:
        return SeasonUserCompletionResult(
            rejected_proof_count=rejected_proof_count,
            finalized=False,
            issued_now=False,
        )

    user = await lock_settlement_user(session, target.season_user_id)
    if user is None or user.user_id != target.user_id:
        raise SeasonCompletionConsistencyError
    progress = await fetch_settlement_progress(session, user.id)
    if progress.selected_project_count != user.required_project_count:
        raise SettlementProgressConsistencyError(
            f"season_user_id={user.id} 有效项目数量不一致"
        )

    finalized = False
    if user.final_points is None:
        finalized = await finalize_settlement_user(
            session,
            user,
            progress.completed_project_count,
        )
        if not finalized:
            raise SeasonCompletionConflictError
    issuance = await issue_season_points_in_transaction(
        session,
        user.id,
    )
    return SeasonUserCompletionResult(
        rejected_proof_count=rejected_proof_count,
        finalized=finalized,
        issued_now=issuance.issued_now,
    )


# 原子完成唯一结算赛季；自动任务可传预期赛季，防止锁定前状态切换误收口新赛季。
async def complete_settling_season(
    session: AsyncSession,
    expected_season_id: int | None = None,
) -> SeasonCompletionResult:
    async with session.begin():
        seasons = await fetch_settling_seasons(session)
        if len(seasons) > 1:
            raise MultipleSettlingSeasonsError
        if not seasons:
            raise SettlingSeasonNotFoundError
        season = seasons[0]
        if expected_season_id is not None and season.id != expected_season_id:
            raise SeasonCompletionConflictError
        initial_targets = await fetch_season_user_finalization_targets(
            session,
            season.id,
        )

        for user_id in sorted({target.user_id for target in initial_targets}):
            if not await lock_user_for_point_update(session, user_id):
                raise SeasonCompletionConsistencyError

        locked_seasons = await lock_settling_seasons(session)
        if len(locked_seasons) > 1:
            raise MultipleSettlingSeasonsError
        if not locked_seasons or locked_seasons[0].id != season.id:
            raise SeasonCompletionConflictError
        locked_targets = await fetch_season_user_finalization_targets(
            session,
            season.id,
            for_update=True,
        )
        if locked_targets != initial_targets:
            raise SeasonCompletionConflictError

        rejected_proof_count = 0
        finalized_user_count = 0
        issued_user_count = 0
        for target in locked_targets:
            result = await complete_season_user_in_transaction(
                session,
                season,
                target,
            )
            rejected_proof_count += result.rejected_proof_count
            finalized_user_count += int(result.finalized)
            issued_user_count += int(result.issued_now)

        await clear_supplement_eligibilities(session)
        if not await mark_season_ended_if_complete(session, season.id):
            raise SeasonCompletionConsistencyError
        return SeasonCompletionResult(
            season_id=season.id,
            participant_count=sum(
                int(target.is_formal_participant)
                for target in locked_targets
            ),
            rejected_proof_count=rejected_proof_count,
            finalized_user_count=finalized_user_count,
            issued_user_count=issued_user_count,
            season_ended=True,
        )


# 在调用方事务内收敛一个正式参赛用户，零完成、资格登记和定分共享同一用户行锁。
async def settle_locked_user(
    session: AsyncSession,
    season_user_id: int,
) -> UserSettlementResult:
    user = await lock_settlement_user(session, season_user_id)
    if user is None or user.final_points is not None:
        return UserSettlementResult()

    await prune_redundant_final_reviews(session, user)
    progress = await fetch_settlement_progress(session, user.id)
    if progress.selected_project_count != user.required_project_count:
        raise SettlementProgressConsistencyError(
            f"season_user_id={user.id} 有效项目数量不一致"
        )

    await close_approved_supplement_eligibilities(session, user.id)
    pending_final_review_ids = await fetch_pending_final_review_proof_ids(
        session,
        user.id,
    )
    has_active_eligibility = await has_active_supplement_eligibility(
        session,
        user.id,
    )

    if has_active_eligibility:
        if (
            not pending_final_review_ids
            and progress.completed_project_count
            == user.required_project_count
        ):
            await close_user_supplement_eligibilities(session, user.id)
        else:
            return UserSettlementResult()
    if pending_final_review_ids:
        if progress.completed_project_count == 0:
            await reject_zero_completion_proofs(
                session,
                user.id,
                pending_final_review_ids,
            )
            finalized = await finalize_settlement_user(
                session,
                user,
                0,
            )
            return UserSettlementResult(finalized=finalized)
        await upsert_supplement_eligibilities(
            session,
            user.id,
            pending_final_review_ids,
        )
        return UserSettlementResult(
            created_eligibility_count=len(pending_final_review_ids)
        )

    finalized = await finalize_settlement_user(
        session,
        user,
        progress.completed_project_count,
    )
    return UserSettlementResult(finalized=finalized)


# 使用独立事务处理一个用户，供定时任务在用户间隔离脏数据和回滚范围。
async def settle_one_user(
    session: AsyncSession,
    season_user_id: int,
) -> UserSettlementResult:
    async with session.begin():
        return await settle_locked_user(session, season_user_id)


# 逐页处理未定分用户并为每个用户使用独立事务，使单个脏数据不会扩大回滚范围。
async def settle_users(
    session_factory: async_sessionmaker[AsyncSession],
    season: SettlementSeason,
    batch_size: int,
) -> tuple[int, int]:
    finalized_user_count = 0
    created_eligibility_count = 0
    after_id = 0
    while True:
        async with session_factory() as session:
            async with session.begin():
                season_user_ids = await fetch_unsettled_season_user_ids(
                    session,
                    season.id,
                    after_id,
                    batch_size,
                )
        if not season_user_ids:
            break
        after_id = season_user_ids[-1]
        for season_user_id in season_user_ids:
            try:
                async with session_factory() as session:
                    result = await settle_one_user(session, season_user_id)
            except Exception:
                logger.exception(
                    "赛季用户结算失败 season_id=%s season_user_id=%s",
                    season.id,
                    season_user_id,
                )
                continue
            finalized_user_count += int(result.finalized)
            created_eligibility_count += result.created_eligibility_count
    return finalized_user_count, created_eligibility_count


# 在独立事务中尝试结束已经完全定分且没有开放补传资格的赛季。
async def finish_season_if_complete(
    session: AsyncSession,
    season_id: int,
) -> bool:
    async with session.begin():
        return await mark_season_ended_if_complete(session, season_id)


# 执行一轮可恢复结算：初始化、清理遗留初审、用户定分和最终状态收敛按顺序隔离。
async def run_season_settlement_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    client_backend: ClientBackendClient,
    business_date: date,
    review_batch_size: int,
    review_concurrency: int,
    user_batch_size: int,
) -> SettlementCycleResult:
    async with session_factory() as session:
        transitioned_season = await initialize_expired_season(
            session,
            business_date,
        )
    async with session_factory() as session:
        settling_season = await get_single_settling_season(session)
    if settling_season is None:
        return SettlementCycleResult(
            transitioned_season_id=(
                transitioned_season.id if transitioned_season else None
            ),
            settling_season_id=None,
            pending_initial_review_count=0,
            finalized_user_count=0,
            created_eligibility_count=0,
            season_ended=False,
        )

    pending_count = await drain_pending_initial_reviews(
        session_factory,
        client_backend,
        settling_season,
        review_batch_size,
        review_concurrency,
    )
    if pending_count > 0:
        return SettlementCycleResult(
            transitioned_season_id=(
                transitioned_season.id if transitioned_season else None
            ),
            settling_season_id=settling_season.id,
            pending_initial_review_count=pending_count,
            finalized_user_count=0,
            created_eligibility_count=0,
            season_ended=False,
        )

    pending_count = await drain_pending_supplement_reviews(
        session_factory,
        client_backend,
        settling_season,
        review_batch_size,
        review_concurrency,
    )
    if pending_count > 0:
        return SettlementCycleResult(
            transitioned_season_id=(
                transitioned_season.id if transitioned_season else None
            ),
            settling_season_id=settling_season.id,
            pending_initial_review_count=pending_count,
            finalized_user_count=0,
            created_eligibility_count=0,
            season_ended=False,
        )

    finalized_count, eligibility_count = await settle_users(
        session_factory,
        settling_season,
        user_batch_size,
    )
    async with session_factory() as session:
        season_ended = await finish_season_if_complete(
            session,
            settling_season.id,
        )
    return SettlementCycleResult(
        transitioned_season_id=(
            transitioned_season.id if transitioned_season else None
        ),
        settling_season_id=settling_season.id,
        pending_initial_review_count=0,
        finalized_user_count=finalized_count,
        created_eligibility_count=eligibility_count,
        season_ended=season_ended,
    )
