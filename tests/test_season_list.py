import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.seasons import SeasonRecord, fetch_all_seasons
from app.router.dependencies import require_admin_token
from app.services.seasons import SeasonListItem, UnknownSeasonStatusError


class FakeMappingsResult:
    # 保存赛季仓储测试使用的预设映射行。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以模拟 SQLAlchemy 的映射结果调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部赛季映射行并保持数据库排序结果。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存预设查询结果，并捕获仓储执行的 SQL 语句。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None

    # 模拟赛季列表的单次异步数据库查询。
    async def exec(self, statement: object) -> FakeMappingsResult:
        self.statement = statement
        return self.result


class SeasonRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储返回所有状态的赛季，并按日期和主键设置稳定倒序。
    async def test_repository_maps_all_seasons(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "id": 2,
                    "name": "2026年9月赛季",
                    "start_date": date(2026, 9, 1),
                    "end_date": date(2026, 9, 30),
                    "status": 0,
                },
                {
                    "id": 1,
                    "name": "2026年8月赛季",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 31),
                    "status": 1,
                },
            ]
        )

        seasons = await fetch_all_seasons(session)  # type: ignore[arg-type]

        self.assertEqual(
            seasons,
            (
                SeasonRecord(
                    id=2,
                    name="2026年9月赛季",
                    start_date=date(2026, 9, 1),
                    end_date=date(2026, 9, 30),
                    status=0,
                ),
                SeasonRecord(
                    id=1,
                    name="2026年8月赛季",
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 8, 31),
                    status=1,
                ),
            ),
        )
        sql = str(session.statement)
        self.assertNotIn("WHERE", sql)
        self.assertIn("season.status", sql)
        self.assertIn("season.start_date DESC", sql)
        self.assertIn("season.end_date DESC", sql)
        self.assertIn("season.id DESC", sql)

    # 验证数据库没有赛季时返回空集合，不构造虚假的默认赛季。
    async def test_repository_returns_empty_seasons(self) -> None:
        session = FakeRepositorySession([])

        seasons = await fetch_all_seasons(session)  # type: ignore[arg-type]

        self.assertEqual(seasons, ())


class SeasonListRouteTestCase(unittest.TestCase):
    # 为接口测试绕过已单独验证的认证，并注入不会访问开发库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回赛季列表路由测试专用的数据库会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止赛季列表测试影响其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口返回赛季 ID、名称和明确的起止日期字段。
    def test_season_list_returns_all_seasons(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                SeasonListItem(
                    id=2,
                    name="2026年9月赛季",
                    start_date=date(2026, 9, 1),
                    end_date=date(2026, 9, 30),
                    status=2,
                    status_name="结算中",
                ),
            )
        )

        with patch(
            "app.router.season.list_seasons_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/season/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": 2,
                    "name": "2026年9月赛季",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-30",
                    "status": 2,
                    "status_name": "结算中",
                }
            ],
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证没有赛季时返回空数组和成功状态，便于前端直接渲染空态。
    def test_season_list_returns_empty_array(self) -> None:
        with patch(
            "app.router.season.list_seasons_service",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/season/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证未知数据库状态转换为明确服务异常，接口不返回猜测的状态含义。
    def test_season_list_reports_unknown_status(self) -> None:
        with patch(
            "app.router.season.list_seasons_service",
            new=AsyncMock(side_effect=UnknownSeasonStatusError),
        ):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/flame/admin/api/season/list")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "赛季状态数据异常"})

    # 验证接口继承统一管理员认证，未登录时不得触发赛季数据库查询。
    def test_season_list_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.season.list_seasons_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season/list",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
