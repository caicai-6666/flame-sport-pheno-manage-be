import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.projects import (
    ProjectInformation,
    fetch_all_projects,
    lock_visible_project_count,
)
from app.router.dependencies import require_admin_token


class FakeMappingsResult:
    # 保存项目仓储测试预设的映射行。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以模拟 SQLAlchemy 映射结果调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设项目行并保持顺序。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存预设查询结果，并捕获项目仓储执行的 SQL。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None

    # 模拟单次异步项目列表查询。
    async def exec(self, statement: object) -> FakeMappingsResult:
        self.statement = statement
        return self.result


class ProjectRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储映射可见与隐藏项目、保留空图标并采用稳定排序条件。
    async def test_repository_maps_all_projects(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "project_id": 1,
                    "project_name": "跑步",
                    "description": "累计跑步里程，提升心肺能力",
                    "icon_url": "/running.png",
                    "status": 1,
                },
                {
                    "project_id": 2,
                    "project_name": "健身",
                    "description": None,
                    "icon_url": None,
                    "status": 0,
                },
            ]
        )

        projects = await fetch_all_projects(session)  # type: ignore[arg-type]

        self.assertEqual(
            projects,
            (
                ProjectInformation(
                    project_id=1,
                    project_name="跑步",
                    description="累计跑步里程，提升心肺能力",
                    icon_url="/running.png",
                    status=1,
                ),
                ProjectInformation(
                    project_id=2,
                    project_name="健身",
                    description=None,
                    icon_url=None,
                    status=0,
                ),
            ),
        )
        sql = str(session.statement)
        self.assertIn("project.description", sql)
        self.assertIn("project.status", sql)
        self.assertNotIn("WHERE", sql)
        self.assertIn("ORDER BY project.id ASC", sql)

    # 验证项目表为空时返回空集合而不是伪造默认项目。
    async def test_repository_returns_empty_projects(self) -> None:
        session = FakeRepositorySession([])

        projects = await fetch_all_projects(session)  # type: ignore[arg-type]

        self.assertEqual(projects, ())

    # 验证赛季创建容量查询仅锁定启用项目，并返回当前可见项目数量。
    async def test_repository_locks_and_counts_visible_projects(self) -> None:
        session = FakeRepositorySession([{"id": 1}, {"id": 2}])

        project_count = await lock_visible_project_count(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(project_count, 2)
        sql = str(session.statement)
        self.assertIn("project.status = 1", sql)
        self.assertIn("FOR SHARE", sql)


class ProjectListRouteTestCase(unittest.TestCase):
    # 为路由测试绕过管理员认证，并注入不访问开发数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前项目路由测试专用会话。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止项目测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口同时返回可见和隐藏项目，并携带状态供前端过滤。
    def test_project_list_returns_all_projects(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                ProjectInformation(
                    project_id=1,
                    project_name="跑步",
                    description="累计跑步里程，提升心肺能力",
                    icon_url="/running.png",
                    status=1,
                ),
                ProjectInformation(
                    project_id=2,
                    project_name="健身",
                    description=None,
                    icon_url=None,
                    status=0,
                ),
            )
        )

        with patch(
            "app.router.project.list_projects_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/project/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "project_id": 1,
                    "project_name": "跑步",
                    "description": "累计跑步里程，提升心肺能力",
                    "icon_url": "/running.png",
                    "status": 1,
                },
                {
                    "project_id": 2,
                    "project_name": "健身",
                    "description": None,
                    "icon_url": None,
                    "status": 0,
                },
            ],
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证项目表为空时接口返回空数组和成功状态。
    def test_project_list_returns_empty_array(self) -> None:
        with patch(
            "app.router.project.list_projects_service",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/project/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证项目列表继承统一管理员认证，未登录请求不会查询数据库。
    def test_project_list_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project.list_projects_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project/list",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
