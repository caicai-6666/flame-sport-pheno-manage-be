import unittest
from datetime import date
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.seasons import (
    CreatedSeasonRecord,
    LatestSeasonBoundary,
    insert_season,
    lock_latest_season_boundary,
)
from app.router.dependencies import require_admin_token
from app.services.seasons import (
    CreatedSeason,
    InsufficientVisibleProjectsError,
    InvalidSeasonDateRangeError,
    SeasonStartDateConflictError,
    calculate_minimum_season_end_date,
    create_season,
)


class FakeMappingsResult:
    # 保存最新赛季边界查询使用的单行结果。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    # 返回自身以模拟 SQLAlchemy 映射结果调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回最新赛季边界，空表时返回 None。
    def first(self) -> dict[str, object] | None:
        return self.row


class FakeInsertResult:
    # 保存数据库为新赛季生成的自增主键。
    def __init__(self, lastrowid: int) -> None:
        self.lastrowid = lastrowid


class FakeRepositorySession:
    # 保存边界查询和插入结果，并捕获两类 SQL 及绑定参数。
    def __init__(
        self,
        boundary_row: dict[str, object] | None = None,
        lastrowid: int = 8,
    ) -> None:
        self.boundary_result = FakeMappingsResult(boundary_row)
        self.insert_result = FakeInsertResult(lastrowid)
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 根据 SQL 类型返回边界或插入替身，确保仓储测试不连接开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult | FakeInsertResult:
        self.statements.append(statement)
        self.params.append(params)
        if "INSERT INTO season" in str(statement):
            return self.insert_result
        return self.boundary_result


class FakeTransactionContext:
    # 记录创建用例是否进入事务及退出时收到的异常。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记赛季创建事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出并保留异常传播，以模拟真实提交或回滚行为。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        self.exception_type = exception_type
        return False


class FakeServiceSession:
    # 为每个赛季创建用例提供可观察的独立事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前用例的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class SeasonCreationRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证边界查询锁定结束日期最晚的赛季，供并发创建串行校验。
    async def test_repository_locks_latest_season_boundary(self) -> None:
        session = FakeRepositorySession(
            boundary_row={"id": 3, "end_date": date(2026, 8, 31)}
        )

        boundary = await lock_latest_season_boundary(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(
            boundary,
            LatestSeasonBoundary(id=3, end_date=date(2026, 8, 31)),
        )
        sql = str(session.statements[0])
        self.assertIn("ORDER BY season.end_date DESC", sql)
        self.assertIn("LIMIT 1", sql)
        self.assertIn("FOR UPDATE", sql)

    # 验证空表边界查询返回 None，允许创建第一个赛季。
    async def test_repository_returns_none_without_existing_season(
        self,
    ) -> None:
        session = FakeRepositorySession()

        boundary = await lock_latest_season_boundary(  # type: ignore[arg-type]
            session
        )

        self.assertIsNone(boundary)

    # 验证插入使用请求指定的必选项目数量、未开始状态和数据库自增主键。
    async def test_repository_inserts_requested_project_count(self) -> None:
        session = FakeRepositorySession(lastrowid=8)

        season = await insert_season(  # type: ignore[arg-type]
            session,
            "2026年9月赛季",
            date(2026, 9, 1),
            date(2026, 9, 30),
            2,
        )

        self.assertEqual(
            season,
            CreatedSeasonRecord(
                id=8,
                name="2026年9月赛季",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30),
                required_project_count=2,
                status=0,
            ),
        )
        sql = str(session.statements[0])
        self.assertIn("INSERT INTO season", sql)
        self.assertIn("required_project_count", sql)
        self.assertIn("status", sql)
        self.assertEqual(
            session.params[0],
            {
                "name": "2026年9月赛季",
                "start_date": date(2026, 9, 1),
                "end_date": date(2026, 9, 30),
                "required_project_count": 2,
            },
        )


class SeasonCreationServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证一个完整日历月按包含首尾日期计算，并兼容跨年日期。
    def test_minimum_end_date_uses_inclusive_calendar_month(self) -> None:
        self.assertEqual(
            calculate_minimum_season_end_date(date(2026, 8, 1)),
            date(2026, 8, 31),
        )
        self.assertEqual(
            calculate_minimum_season_end_date(date(2026, 12, 15)),
            date(2027, 1, 14),
        )

    # 验证可见项目数量恰好满足要求时，事务内创建默认未开始赛季。
    async def test_create_season_after_latest_boundary(self) -> None:
        session = FakeServiceSession()
        boundary = LatestSeasonBoundary(3, date(2026, 8, 31))
        inserted = CreatedSeasonRecord(
            id=8,
            name="2026年9月赛季",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            required_project_count=2,
            status=0,
        )
        boundary_mock = AsyncMock(return_value=boundary)
        project_count_mock = AsyncMock(return_value=2)
        insert_mock = AsyncMock(return_value=inserted)

        with (
            patch(
                "app.services.seasons.lock_latest_season_boundary",
                new=boundary_mock,
            ),
            patch(
                "app.services.seasons.lock_visible_project_count",
                new=project_count_mock,
            ),
            patch(
                "app.services.seasons.insert_season",
                new=insert_mock,
            ),
        ):
            season = await create_season(  # type: ignore[arg-type]
                session,
                "2026年9月赛季",
                date(2026, 9, 1),
                date(2026, 9, 30),
                2,
            )

        self.assertEqual(
            season,
            CreatedSeason(
                id=8,
                name="2026年9月赛季",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30),
                required_project_count=2,
                status=0,
                status_name="未开始",
            ),
        )
        boundary_mock.assert_awaited_once_with(session)
        project_count_mock.assert_awaited_once_with(session)
        insert_mock.assert_awaited_once_with(
            session,
            "2026年9月赛季",
            date(2026, 9, 1),
            date(2026, 9, 30),
            2,
        )
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证少于完整日历月时在开启事务前拒绝，避免无意义加锁。
    async def test_create_season_rejects_short_date_range(self) -> None:
        session = FakeServiceSession()

        with self.assertRaises(InvalidSeasonDateRangeError):
            await create_season(  # type: ignore[arg-type]
                session,
                "短赛季",
                date(2026, 9, 1),
                date(2026, 9, 29),
                2,
            )

        self.assertFalse(session.transaction.entered)

    # 验证开始日期等于或早于最晚结束日期时回滚事务且不执行插入。
    async def test_create_season_rejects_overlapping_boundary(self) -> None:
        session = FakeServiceSession()
        insert_mock = AsyncMock()

        with (
            patch(
                "app.services.seasons.lock_latest_season_boundary",
                new=AsyncMock(
                    return_value=LatestSeasonBoundary(
                        3,
                        date(2026, 8, 31),
                    )
                ),
            ),
            patch(
                "app.services.seasons.insert_season",
                new=insert_mock,
            ),
        ):
            with self.assertRaises(SeasonStartDateConflictError):
                await create_season(  # type: ignore[arg-type]
                    session,
                    "重叠赛季",
                    date(2026, 8, 31),
                    date(2026, 9, 30),
                    2,
                )

        insert_mock.assert_not_awaited()
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)
        self.assertIs(
            session.transaction.exception_type,
            SeasonStartDateConflictError,
        )

    # 验证要求项目数超过当前可见项目数时回滚事务且不写入赛季。
    async def test_create_season_rejects_insufficient_visible_projects(
        self,
    ) -> None:
        session = FakeServiceSession()
        insert_mock = AsyncMock()

        with (
            patch(
                "app.services.seasons.lock_latest_season_boundary",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.seasons.lock_visible_project_count",
                new=AsyncMock(return_value=1),
            ),
            patch(
                "app.services.seasons.insert_season",
                new=insert_mock,
            ),
        ):
            with self.assertRaises(InsufficientVisibleProjectsError):
                await create_season(  # type: ignore[arg-type]
                    session,
                    "项目不足赛季",
                    date(2026, 9, 1),
                    date(2026, 9, 30),
                    2,
                )

        insert_mock.assert_not_awaited()
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)
        self.assertIs(
            session.transaction.exception_type,
            InsufficientVisibleProjectsError,
        )


class SeasonCreationRouteTestCase(unittest.TestCase):
    # 为创建接口测试绕过认证并注入不会访问开发数据库的会话替身。
    def setUp(self) -> None:
        self.session = object()

        # 返回赛季创建路由测试专用会话。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止创建接口测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证合法请求返回 201、请求指定的必选项目数量和默认未开始状态。
    def test_create_season_returns_created_season(self) -> None:
        service_mock = AsyncMock(
            return_value=CreatedSeason(
                id=8,
                name="2026年9月赛季",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 30),
                required_project_count=2,
                status=0,
                status_name="未开始",
            )
        )

        with patch(
            "app.router.season.create_season_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/season/create",
                    json={
                        "name": " 2026年9月赛季 ",
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-30",
                        "required_project_count": 2,
                    },
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "id": 8,
                "name": "2026年9月赛季",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "required_project_count": 2,
                "status": 0,
                "status_name": "未开始",
            },
        )
        service_mock.assert_awaited_once_with(
            self.session,
            "2026年9月赛季",
            date(2026, 9, 1),
            date(2026, 9, 30),
            2,
        )

    # 验证日期错误和可见项目容量不足映射为稳定的 422 或 409 响应。
    def test_create_season_maps_business_errors(self) -> None:
        cases = (
            (
                InvalidSeasonDateRangeError,
                422,
                "赛季周期不能少于一个完整日历月",
            ),
            (
                SeasonStartDateConflictError,
                409,
                "赛季开始日期必须晚于已有赛季的最晚结束日期",
            ),
            (
                InsufficientVisibleProjectsError,
                409,
                "要求项目个数不能超过当前可见项目个数",
            ),
        )

        for error_type, expected_status, expected_detail in cases:
            with self.subTest(error_type=error_type):
                with patch(
                    "app.router.season.create_season_service",
                    new=AsyncMock(side_effect=error_type),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/flame/admin/api/season/create",
                            json={
                                "name": "2026年9月赛季",
                                "start_date": "2026-09-01",
                                "end_date": "2026-09-30",
                                "required_project_count": 2,
                            },
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证名称、日期及必选项目数量的非法输入由请求模型统一拒绝。
    def test_create_season_validates_request_fields(self) -> None:
        invalid_payloads = (
            {
                "name": "   ",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "required_project_count": 2,
            },
            {
                "name": "合法名称",
                "start_date": "invalid-date",
                "end_date": "2026-09-30",
                "required_project_count": 2,
            },
            {
                "name": "赛" * 65,
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "required_project_count": 2,
            },
            {
                "name": "合法名称",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "required_project_count": 0,
            },
            {
                "name": "合法名称",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "required_project_count": 256,
            },
            {
                "name": "合法名称",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
            },
        )

        with patch(
            "app.router.season.create_season_service",
            new=AsyncMock(),
        ) as service_mock:
            with TestClient(app) as client:
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.post(
                            "/flame/admin/api/season/create",
                            json=payload,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证创建接口受管理员认证保护，未登录请求不会进入写服务。
    def test_create_season_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.season.create_season_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/season/create",
                    json={
                        "name": "2026年9月赛季",
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-30",
                        "required_project_count": 2,
                    },
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
