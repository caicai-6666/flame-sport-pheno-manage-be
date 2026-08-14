import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.router.dependencies import require_admin_token
from app.repositories.season_statistics import (
    CurrentSeasonParticipant,
    CurrentSeasonProjectParticipant,
    CurrentSeasonStatistics,
    MultipleActiveSeasonsError,
    fetch_current_season_project_participants,
    fetch_current_season_statistics,
)
from app.services.season_statistics import (
    CurrentSeasonConflictError,
    CurrentSeasonNotFoundError,
)


class FakeMappingsResult:
    # 保存预设查询行，模拟 SQLAlchemy mappings 结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持 result.mappings().all() 调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部映射行，保持查询结果顺序不变。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存预设数据库行，并记录仓储发出的 SQL 语句。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None
        self.params = None

    # 模拟 SQLModel 异步查询并捕获语句，确保仓储只执行一次聚合查询。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class CurrentSeasonStatisticsRepositoryTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证仓储只映射报名条件完整的正式参赛人员，并排除等级为空的记录。
    async def test_repository_maps_current_season_and_participants(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "season_id": 7,
                    "season_name": "2026年8月赛季",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 31),
                    "required_project_count": 3,
                    "season_status": 1,
                    "season_user_id": 101,
                    "user_id": "user-1",
                    "level_id": 2,
                    "level_name": "白银",
                },
                {
                    "season_id": 7,
                    "season_name": "2026年8月赛季",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 31),
                    "required_project_count": 3,
                    "season_status": 1,
                    "season_user_id": 102,
                    "user_id": "user-2",
                    "level_id": None,
                    "level_name": None,
                },
            ]
        )

        current_season = await fetch_current_season_statistics(session)  # type: ignore[arg-type]

        self.assertIsNotNone(current_season)
        self.assertEqual(current_season.id, 7)  # type: ignore[union-attr]
        self.assertEqual(
            current_season.participants,  # type: ignore[union-attr]
            (
                CurrentSeasonParticipant(
                    season_user_id=101,
                    user_id="user-1",
                    level_id=2,
                    level_name="白银",
                ),
            ),
        )
        sql = str(session.statement)
        self.assertIn("season.status = 1", sql)
        self.assertIn(
            "season_user.status >= season.required_project_count",
            sql,
        )
        self.assertIn(
            "project_level.id = season_user.level_id",
            sql,
        )
        self.assertIn("season_user.level_id IS NOT NULL", sql)
        self.assertIn("season_user.id AS season_user_id", sql)
        self.assertNotIn("season_user.participated_at IS NOT NULL", sql)

    # 验证没有激活赛季时仓储返回空结果，而不是伪造默认赛季。
    async def test_repository_returns_none_without_active_season(self) -> None:
        session = FakeRepositorySession([])

        current_season = await fetch_current_season_statistics(session)  # type: ignore[arg-type]

        self.assertIsNone(current_season)

    # 验证激活赛季没有正式参赛人员时仍返回赛季，并将人员列表映射为空。
    async def test_repository_returns_empty_participants(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "season_id": 7,
                    "season_name": "2026年8月赛季",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 31),
                    "required_project_count": 3,
                    "season_status": 1,
                    "season_user_id": None,
                    "user_id": None,
                    "level_id": None,
                    "level_name": None,
                }
            ]
        )

        current_season = await fetch_current_season_statistics(session)  # type: ignore[arg-type]

        self.assertIsNotNone(current_season)
        self.assertEqual(current_season.participants, ())  # type: ignore[union-attr]

    # 验证多个激活赛季触发一致性异常，禁止静默选择其中一条。
    async def test_repository_rejects_multiple_active_seasons(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "season_id": 7,
                    "season_name": "赛季一",
                    "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 31),
                    "required_project_count": 3,
                    "season_status": 1,
                    "season_user_id": None,
                    "user_id": None,
                    "level_id": None,
                    "level_name": None,
                },
                {
                    "season_id": 8,
                    "season_name": "赛季二",
                    "start_date": date(2026, 9, 1),
                    "end_date": date(2026, 9, 30),
                    "required_project_count": 3,
                    "season_status": 1,
                    "season_user_id": None,
                    "user_id": None,
                    "level_id": None,
                    "level_name": None,
                },
            ]
        )

        with self.assertRaises(MultipleActiveSeasonsError):
            await fetch_current_season_statistics(session)  # type: ignore[arg-type]


class CurrentSeasonStatisticsRouteTestCase(unittest.TestCase):
    # 为接口测试绕过已单独验证的管理员认证，并注入不访问真实数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前测试专用会话，避免路由测试连接开发数据库。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止路由测试替身影响其他测试模块。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口返回当前赛季，并为每位正式参赛人员提供赛季用户记录主键。
    def test_current_endpoint_returns_season_and_participants(self) -> None:
        current_season = CurrentSeasonStatistics(
            id=7,
            name="2026年8月赛季",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            required_project_count=3,
            status=1,
            participants=(
                CurrentSeasonParticipant(
                    season_user_id=101,
                    user_id="user-1",
                    level_id=2,
                    level_name="白银",
                ),
            ),
        )

        service_mock = AsyncMock(return_value=current_season)
        with patch(
            "app.router.season_statistics.get_current_season_statistics",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/current"
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": 7,
                "name": "2026年8月赛季",
                "start_date": "2026-08-01",
                "end_date": "2026-08-31",
                "required_project_count": 3,
                "status": 1,
                "participants": [
                    {
                        "season_user_id": 101,
                        "user_id": "user-1",
                        "level_id": 2,
                        "level_name": "白银",
                    }
                ],
            },
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证当前没有激活赛季时返回 404 和可供前端展示的明确提示。
    def test_current_endpoint_returns_not_found_without_active_season(
        self,
    ) -> None:
        with patch(
            "app.router.season_statistics.get_current_season_statistics",
            new=AsyncMock(side_effect=CurrentSeasonNotFoundError),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/current"
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "当前没有激活的赛季"})

    # 验证多个激活赛季作为数据一致性问题返回 409，不泄露 SQL 细节。
    def test_current_endpoint_returns_conflict_for_multiple_seasons(
        self,
    ) -> None:
        with patch(
            "app.router.season_statistics.get_current_season_statistics",
            new=AsyncMock(side_effect=CurrentSeasonConflictError),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/current"
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": "存在多个激活赛季，无法确定当前赛季"},
        )

    # 验证接口仍受统一管理员依赖保护，未登录请求不会进入数据库查询。
    def test_current_endpoint_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.season_statistics.get_current_season_statistics",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/current",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()


class CurrentSeasonProjectParticipantsRepositoryTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证仓储按当前赛季、参赛记录和项目过滤，并保留数据库小数进度。
    async def test_repository_maps_project_participant_progress(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "user_id": "user-1",
                    "completion_progress": Decimal("0.7500"),
                }
            ]
        )

        participants = await fetch_current_season_project_participants(  # type: ignore[arg-type]
            session,
            season_user_id=101,
            project_id=5,
        )

        self.assertEqual(
            participants,
            (
                CurrentSeasonProjectParticipant(
                    user_id="user-1",
                    completion_progress=Decimal("0.7500"),
                ),
            ),
        )
        self.assertEqual(
            session.params,
            {"season_user_id": 101, "project_id": 5},
        )
        sql = str(session.statement)
        self.assertIn("FROM season_user_project", sql)
        self.assertIn("season.status = 1", sql)
        self.assertIn("season_user_project.status = 1", sql)
        self.assertIn(
            "season_user.status >= season.required_project_count",
            sql,
        )
        self.assertIn("season_user.level_id IS NOT NULL", sql)

    # 验证没有匹配的当前赛季有效项目时仓储返回空列表语义。
    async def test_repository_returns_empty_without_matching_project(
        self,
    ) -> None:
        session = FakeRepositorySession([])

        participants = await fetch_current_season_project_participants(  # type: ignore[arg-type]
            session,
            season_user_id=101,
            project_id=5,
        )

        self.assertEqual(participants, ())


class CurrentSeasonProjectParticipantsRouteTestCase(unittest.TestCase):
    # 为项目参赛进度路由绕过认证，并注入不会连接开发数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前接口测试专用会话，数据库行为由服务替身隔离。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止项目参赛接口测试影响其他模块。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口返回用户 ID 和范围为零到一的项目完成进度。
    def test_project_participants_returns_user_progress(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                CurrentSeasonProjectParticipant(
                    user_id="user-1",
                    completion_progress=Decimal("0.7500"),
                ),
            )
        )

        with patch(
            "app.router.season_statistics."
            "get_current_season_project_participants",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/project-participants",
                    params={"season_user_id": 101, "project_id": 5},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [{"user_id": "user-1", "completion_progress": 0.75}],
        )
        service_mock.assert_awaited_once_with(self.session, 101, 5)

    # 验证没有匹配条目时返回空数组，而不是伪造人员或返回 404。
    def test_project_participants_returns_empty_array(self) -> None:
        with patch(
            "app.router.season_statistics."
            "get_current_season_project_participants",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/project-participants",
                    params={"season_user_id": 101, "project_id": 5},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证缺少参数或传入非正整数时由 HTTP 边界拒绝无效查询。
    def test_project_participants_validates_identifiers(self) -> None:
        with TestClient(app) as client:
            missing_response = client.get(
                "/flame/admin/api/season-statistics/project-participants"
            )
            invalid_response = client.get(
                "/flame/admin/api/season-statistics/project-participants",
                params={"season_user_id": 0, "project_id": -1},
            )

        self.assertEqual(missing_response.status_code, 422)
        self.assertEqual(invalid_response.status_code, 422)

    # 验证接口继承统一认证，未登录请求不会调用项目参赛查询服务。
    def test_project_participants_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.season_statistics."
            "get_current_season_project_participants",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/season-statistics/project-participants",
                    params={"season_user_id": 101, "project_id": 5},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
