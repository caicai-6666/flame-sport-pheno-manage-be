import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.projects import (
    ProjectInformation,
    update_project_visibility_status as update_project_visibility_status_repository,
)
from app.router.dependencies import require_admin_token
from app.services.configuration_guard import (
    ActiveSeasonConfigurationWindowClosedError,
    MultipleActiveSeasonsForConfigurationError,
)
from app.services.projects import (
    ProjectNotFoundError,
    update_project_visibility_status as update_project_visibility_status_service,
)


class FakeMappingsResult:
    # 保存项目状态仓储测试预设行，以模拟唯一项目查询结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持仓储使用 mappings() 读取映射结果。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回首条项目记录，空集合表示项目不存在。
    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class FakeRepositorySession:
    # 按顺序返回项目锁定与状态更新结果，并记录 SQL 和绑定参数。
    def __init__(self, result_rows: list[list[dict[str, object]]]) -> None:
        self.results = [FakeMappingsResult(rows) for rows in result_rows]
        self.statements: list[object] = []
        self.params: list[dict[str, object] | None] = []

    # 模拟项目仓储连续执行的异步数据库语句。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statements.append(statement)
        self.params.append(params)
        if self.results:
            return self.results.pop(0)
        return FakeMappingsResult([])


class FakeTransactionContext:
    # 记录项目状态服务是否进入事务及退出时的异常类型。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记项目状态写事务已经开始。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 记录事务退出结果，并保留异常的正常传播与回滚语义。
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
    # 为项目状态服务提供唯一且可观察的写事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前用例使用的事务上下文，避免访问真实数据库。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProjectStatusRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储先锁定项目，再参数化覆盖状态并返回完整基础信息。
    async def test_repository_updates_project_visibility_status(self) -> None:
        session = FakeRepositorySession(
            [
                [
                    {
                        "project_id": 2,
                        "project_name": "健身打卡",
                        "description": "记录每日训练",
                        "icon_url": "/fitness.png",
                    }
                ],
                [],
            ]
        )

        project = await update_project_visibility_status_repository(  # type: ignore[arg-type]
            session,
            2,
            0,
        )

        self.assertEqual(
            project,
            ProjectInformation(
                project_id=2,
                project_name="健身打卡",
                description="记录每日训练",
                icon_url="/fitness.png",
                status=0,
            ),
        )
        self.assertIn("FOR UPDATE", str(session.statements[0]))
        self.assertIn("UPDATE project", str(session.statements[1]))
        self.assertEqual(session.params[0], {"project_id": 2})
        self.assertEqual(
            session.params[1],
            {"project_id": 2, "visibility_status": 0},
        )

    # 验证项目不存在时仓储不执行状态更新，也不伪造项目结果。
    async def test_repository_reports_missing_project(self) -> None:
        session = FakeRepositorySession([[]])

        project = await update_project_visibility_status_repository(  # type: ignore[arg-type]
            session,
            99,
            0,
        )

        self.assertIsNone(project)
        self.assertEqual(len(session.statements), 1)


class ProjectStatusServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证服务先校验配置窗口，再在同一事务内修改目标项目状态。
    async def test_service_updates_status_in_configuration_window(
        self,
    ) -> None:
        session = FakeServiceSession()
        expected_project = ProjectInformation(
            project_id=2,
            project_name="健身打卡",
            description=None,
            icon_url=None,
            status=0,
        )
        guard_mock = AsyncMock()
        repository_mock = AsyncMock(return_value=expected_project)

        with (
            patch(
                "app.services.projects."
                "ensure_active_season_configuration_editable",
                new=guard_mock,
            ),
            patch(
                "app.services.projects."
                "update_project_visibility_status_repository",
                new=repository_mock,
            ),
        ):
            project = await update_project_visibility_status_service(  # type: ignore[arg-type]
                session,
                2,
                0,
                edit_window_hours=48,
            )

        self.assertEqual(project, expected_project)
        guard_mock.assert_awaited_once_with(session, 48)
        repository_mock.assert_awaited_once_with(session, 2, 0)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证配置窗口关闭时服务不会继续锁定或修改项目记录。
    async def test_service_rejects_status_outside_configuration_window(
        self,
    ) -> None:
        session = FakeServiceSession()
        repository_mock = AsyncMock()

        with (
            patch(
                "app.services.projects."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(
                    side_effect=ActiveSeasonConfigurationWindowClosedError
                ),
            ),
            patch(
                "app.services.projects."
                "update_project_visibility_status_repository",
                new=repository_mock,
            ),
        ):
            with self.assertRaises(
                ActiveSeasonConfigurationWindowClosedError
            ):
                await update_project_visibility_status_service(  # type: ignore[arg-type]
                    session,
                    2,
                    0,
                    edit_window_hours=24,
                )

        repository_mock.assert_not_awaited()
        self.assertIs(
            session.transaction.exception_type,
            ActiveSeasonConfigurationWindowClosedError,
        )

    # 验证项目不存在时服务抛出稳定异常并回滚写事务。
    async def test_service_reports_missing_project(self) -> None:
        session = FakeServiceSession()

        with (
            patch(
                "app.services.projects."
                "ensure_active_season_configuration_editable",
                new=AsyncMock(),
            ),
            patch(
                "app.services.projects."
                "update_project_visibility_status_repository",
                new=AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaises(ProjectNotFoundError):
                await update_project_visibility_status_service(  # type: ignore[arg-type]
                    session,
                    99,
                    0,
                )

        self.assertIs(
            session.transaction.exception_type,
            ProjectNotFoundError,
        )


class ProjectStatusRouteTestCase(unittest.TestCase):
    # 为项目状态路由绕过独立认证测试并注入不访问数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回项目状态路由测试专用会话，真实事务由服务替身隔离。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止项目状态测试污染其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口修改项目可见状态并返回更新后的完整基础信息。
    def test_route_updates_project_visibility_status(self) -> None:
        service_mock = AsyncMock(
            return_value=ProjectInformation(
                project_id=2,
                project_name="健身打卡",
                description="记录每日训练",
                icon_url="/fitness.png",
                status=0,
            )
        )

        with patch(
            "app.router.project.update_project_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/project/2/status",
                    json={"status": 0},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "project_id": 2,
                "project_name": "健身打卡",
                "description": "记录每日训练",
                "icon_url": "/fitness.png",
                "status": 0,
            },
        )
        service_mock.assert_awaited_once_with(self.session, 2, 0)

    # 验证项目不存在及两类配置窗口冲突均映射为稳定 HTTP 响应。
    def test_route_maps_project_status_errors(self) -> None:
        cases = (
            (ProjectNotFoundError, 404, "运动项目不存在"),
            (
                ActiveSeasonConfigurationWindowClosedError,
                409,
                "当前激活赛季的配置修改窗口已关闭",
            ),
            (
                MultipleActiveSeasonsForConfigurationError,
                409,
                "存在多个激活赛季，无法判断配置修改窗口",
            ),
        )

        for error_type, expected_status, expected_detail in cases:
            with self.subTest(error_type=error_type):
                with patch(
                    "app.router.project."
                    "update_project_visibility_status_service",
                    new=AsyncMock(side_effect=error_type),
                ):
                    with TestClient(app) as client:
                        response = client.patch(
                            "/flame/admin/api/project/2/status",
                            json={"status": 0},
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证状态必须是严格整数 0 或 1，且请求不能携带额外字段。
    def test_route_validates_project_visibility_status(self) -> None:
        invalid_payloads = (
            {},
            {"status": -1},
            {"status": 2},
            {"status": True},
            {"status": "1"},
            {"status": 1, "visible": True},
        )
        service_mock = AsyncMock()

        with patch(
            "app.router.project.update_project_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for payload in invalid_payloads:
                    with self.subTest(payload=payload):
                        response = client.patch(
                            "/flame/admin/api/project/2/status",
                            json=payload,
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证项目 ID 必须为正整数，非法路径不会进入业务服务。
    def test_route_validates_project_id(self) -> None:
        service_mock = AsyncMock()

        with patch(
            "app.router.project.update_project_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                for project_id in ("0", "-1", "invalid"):
                    with self.subTest(project_id=project_id):
                        response = client.patch(
                            f"/flame/admin/api/project/{project_id}/status",
                            json={"status": 0},
                        )
                        self.assertEqual(response.status_code, 422)

        service_mock.assert_not_awaited()

    # 验证项目状态修改继承统一管理员认证，未登录请求不会执行写服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project.update_project_visibility_status_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.patch(
                    "/flame/admin/api/project/2/status",
                    json={"status": 0},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
