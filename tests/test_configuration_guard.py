import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tests.support import configure_test_environment

configure_test_environment()

from app.repositories.configuration_guard import (
    ActiveSeasonConfigurationReference,
    lock_active_seasons_for_configuration,
)
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
    ensure_active_season_configuration_editable,
    is_active_season_configuration_window_open,
)


class FakeMappingsResult:
    # 保存激活赛季守卫测试预设的数据库映射行。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以模拟 SQLAlchemy 映射结果调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设赛季并保持仓储查询顺序。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeGuardSession:
    # 保存预设赛季行，并捕获守卫仓储执行的锁定查询。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement: object | None = None

    # 返回预设赛季查询结果，确保测试不连接开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        return self.result


class ConfigurationGuardRepositoryTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证守卫只锁定进行中赛季，并映射判断窗口所需的主键和开始日期。
    async def test_repository_locks_active_seasons(self) -> None:
        session = FakeGuardSession(
            [{"id": 3, "start_date": date(2026, 8, 13)}]
        )

        active_seasons = await lock_active_seasons_for_configuration(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(
            active_seasons,
            (ActiveSeasonConfigurationReference(3, date(2026, 8, 13)),),
        )
        sql = str(session.statement)
        self.assertIn("season.status = 1", sql)
        self.assertIn("FOR SHARE", sql)


class ConfigurationGuardServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证没有激活赛季时允许管理员预先维护高影响业务配置。
    async def test_no_active_season_allows_configuration(self) -> None:
        session = FakeGuardSession([])

        await ensure_active_season_configuration_editable(  # type: ignore[arg-type]
            session,
            24,
            datetime(2026, 8, 13, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    # 验证配置窗口在截止时刻采用左闭右开语义，达到截止点即关闭。
    def test_window_closes_at_deadline(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")

        self.assertTrue(
            is_active_season_configuration_window_open(
                date(2026, 8, 13),
                24,
                datetime(2026, 8, 13, 23, 59, 59, tzinfo=timezone),
            )
        )
        self.assertFalse(
            is_active_season_configuration_window_open(
                date(2026, 8, 13),
                24,
                datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone),
            )
        )

    # 验证唯一激活赛季超过窗口后拒绝写入，并保留明确业务异常。
    async def test_closed_window_rejects_configuration(self) -> None:
        session = FakeGuardSession(
            [{"id": 3, "start_date": date(2026, 8, 1)}]
        )

        with self.assertRaises(
            ActiveSeasonConfigurationWindowClosedError
        ):
            await ensure_active_season_configuration_editable(  # type: ignore[arg-type]
                session,
                24,
                datetime(
                    2026,
                    8,
                    13,
                    12,
                    tzinfo=ZoneInfo("Asia/Shanghai"),
                ),
            )

    # 验证多个激活赛季被视为数据一致性冲突，禁止任意选择一个窗口。
    async def test_multiple_active_seasons_reject_configuration(self) -> None:
        session = FakeGuardSession(
            [
                {"id": 3, "start_date": date(2026, 8, 1)},
                {"id": 4, "start_date": date(2026, 8, 2)},
            ]
        )

        with self.assertRaises(MultipleActiveSeasonsForConfigurationError):
            await ensure_active_season_configuration_editable(  # type: ignore[arg-type]
                session,
                24,
            )
