"""定期推进到期赛季并持续收敛结算流程。"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from app.clients.client_backend import ClientBackendClient
from app.services.season_settlements import (
    SeasonCompletionResult,
    SeasonCompletionConflictError,
    SettlementCycleResult,
    SettlingSeasonNotFoundError,
    activate_due_season,
    complete_settling_season,
    get_single_settling_season,
    run_season_settlement_cycle,
)

APPLICATION_TIMEZONE = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


# 到达配置的赛后业务日期时复用一键结算事务，未到期或没有结算赛季时保持无操作。
async def auto_complete_overdue_settling_season(
    session_factory: async_sessionmaker[AsyncSession],
    business_date: date,
    after_days: int,
) -> SeasonCompletionResult | None:
    async with session_factory() as session:
        season = await get_single_settling_season(session)
    if season is None:
        return None
    auto_complete_date = season.end_date + timedelta(days=after_days)
    if business_date < auto_complete_date:
        return None
    async with session_factory() as session:
        try:
            return await complete_settling_season(
                session,
                expected_season_id=season.id,
            )
        except (
            SettlingSeasonNotFoundError,
            SeasonCompletionConflictError,
        ):
            logger.info(
                "自动一键结算因并发状态变化跳过 season_id=%s",
                season.id,
            )
            return None


# 使用上海业务日期执行结算轮询，并在配置期限到达时追加原子一键收口。
async def check_season_settlement(
    session_factory: async_sessionmaker[AsyncSession],
    client_backend: ClientBackendClient,
    review_batch_size: int,
    review_concurrency: int,
    user_batch_size: int,
    business_date: date | None = None,
    auto_complete_enabled: bool = False,
    auto_complete_after_days: int = 7,
) -> SettlementCycleResult:
    effective_date = business_date or datetime.now(APPLICATION_TIMEZONE).date()
    result = await run_season_settlement_cycle(
        session_factory,
        client_backend,
        effective_date,
        review_batch_size,
        review_concurrency,
        user_batch_size,
    )
    async with session_factory() as session:
        activated_season = await activate_due_season(session, effective_date)
    if activated_season is not None:
        logger.info(
            "赛季已自动进入进行中 season_id=%s",
            activated_season.id,
        )
    if not auto_complete_enabled or result.season_ended:
        return result
    completion = await auto_complete_overdue_settling_season(
        session_factory,
        effective_date,
        auto_complete_after_days,
    )
    if completion is None:
        return result
    logger.info(
        "赛季已自动一键结算 season_id=%s participants=%s rejected=%s "
        "finalized=%s issued=%s",
        completion.season_id,
        completion.participant_count,
        completion.rejected_proof_count,
        completion.finalized_user_count,
        completion.issued_user_count,
    )
    return SettlementCycleResult(
        transitioned_season_id=result.transitioned_season_id,
        settling_season_id=completion.season_id,
        pending_initial_review_count=0,
        finalized_user_count=(
            result.finalized_user_count + completion.finalized_user_count
        ),
        created_eligibility_count=0,
        season_ended=True,
    )


# 启动后立即检查并周期续跑常规及自动收口；失败后重试且取消信号向生命周期传播。
async def run_season_status_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    client_backend: ClientBackendClient,
    interval_seconds: int,
    review_batch_size: int,
    review_concurrency: int,
    user_batch_size: int,
    auto_complete_enabled: bool = False,
    auto_complete_after_days: int = 7,
) -> None:
    while True:
        try:
            result = await check_season_settlement(
                session_factory,
                client_backend,
                review_batch_size,
                review_concurrency,
                user_batch_size,
                auto_complete_enabled=auto_complete_enabled,
                auto_complete_after_days=auto_complete_after_days,
            )
            if result.transitioned_season_id is not None:
                logger.info(
                    "赛季已进入结算中 season_id=%s",
                    result.transitioned_season_id,
                )
            if result.settling_season_id is not None:
                logger.info(
                    "赛季结算轮询完成 season_id=%s pending_initial=%s "
                    "finalized_users=%s created_eligibilities=%s ended=%s",
                    result.settling_season_id,
                    result.pending_initial_review_count,
                    result.finalized_user_count,
                    result.created_eligibility_count,
                    result.season_ended,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise asyncio.CancelledError
            logger.exception("赛季结算定时检查失败，将在下个周期重试")
        await asyncio.sleep(interval_seconds)
