import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.projects import (
    ProjectRuleContent,
    fetch_project_rule_content,
)
from app.router.dependencies import require_admin_token
from app.services.projects import (
    ProjectRuleNotFoundError,
    get_project_rule_content,
)


class FakeMappingsResult:
    # 保存项目规则查询的首行结果，以模拟 SQLAlchemy 映射结果。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    # 返回自身以支持仓储使用 mappings().first() 读取唯一规则。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回预设唯一行，空值表示没有匹配的启用规则。
    def first(self) -> dict[str, object] | None:
        return self.row


class FakeRepositorySession:
    # 保存预设规则行，并记录仓储实际使用的 SQL 与绑定参数。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.result = FakeMappingsResult(row)
        self.statement = None
        self.params = None

    # 模拟参数化异步查询，确保测试不会访问开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class FakeTransactionContext:
    # 初始化可观察事务状态，用于验证服务完整管理事务生命周期。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    # 标记项目规则服务已经进入只读事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务正常或异常退出，并保持业务异常继续传播。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False


class FakeServiceSession:
    # 为服务测试提供单一可观察事务上下文。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前服务用例应使用的只读事务。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class ProjectRuleRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储按联合标识查询对应规则，并将 MySQL JSON 文本还原为数组。
    async def test_repository_returns_decoded_rule_content(self) -> None:
        session = FakeRepositorySession(
            {
                "sub_desc": "提升有氧容量和节奏控制",
                "rule_content": (
                    '[{"label":"累计距离","value":"50km"}]'
                ),
                "rule_note": "跑步或快走均可累计",
            }
        )

        project_rule = await fetch_project_rule_content(  # type: ignore[arg-type]
            session,
            project_id=2,
            level_id=3,
        )

        self.assertEqual(
            project_rule,
            ProjectRuleContent(
                sub_desc="提升有氧容量和节奏控制",
                rule_content=[
                    {"label": "累计距离", "value": "50km"}
                ],
                rule_note="跑步或快走均可累计",
            ),
        )
        self.assertEqual(
            session.params,
            {"project_id": 2, "level_id": 3},
        )
        sql = str(session.statement)
        self.assertIn("project_rule.project_id = :project_id", sql)
        self.assertIn("project_rule.level_id = :level_id", sql)
        self.assertIn("project_rule.sub_desc", sql)
        self.assertIn("project_rule.rule_note", sql)
        self.assertNotIn("project_rule.status", sql)
        self.assertIn("LIMIT 1", sql)

    # 验证没有对应规则时仓储返回空值，不伪造规则内容。
    async def test_repository_returns_none_without_rule(self) -> None:
        session = FakeRepositorySession(None)

        project_rule = await fetch_project_rule_content(  # type: ignore[arg-type]
            session,
            project_id=2,
            level_id=3,
        )

        self.assertIsNone(project_rule)


class ProjectRuleServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证规则查询服务在只读事务内传递两个标识并返回仓储结果。
    async def test_service_manages_transaction(self) -> None:
        session = FakeServiceSession()
        expected_rule = ProjectRuleContent(
            sub_desc=None,
            rule_content=[{"label": "累计距离", "value": "50km"}],
            rule_note=None,
        )
        repository_mock = AsyncMock(return_value=expected_rule)

        with patch(
            "app.services.projects.fetch_project_rule_content",
            new=repository_mock,
        ):
            project_rule = await get_project_rule_content(  # type: ignore[arg-type]
                session,
                project_id=2,
                level_id=3,
            )

        self.assertEqual(project_rule, expected_rule)
        repository_mock.assert_awaited_once_with(session, 2, 3)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证空查询结果转换为稳定业务异常，并正常结束只读事务。
    async def test_service_reports_missing_rule(self) -> None:
        session = FakeServiceSession()

        with patch(
            "app.services.projects.fetch_project_rule_content",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaises(ProjectRuleNotFoundError):
                await get_project_rule_content(  # type: ignore[arg-type]
                    session,
                    project_id=2,
                    level_id=3,
                )

        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class ProjectRuleRouteTestCase(unittest.TestCase):
    # 为规则路由绕过已单独覆盖的认证，并注入不连接数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前规则路由测试专用会话，真实查询由服务替身隔离。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止规则路由测试污染其他接口。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口按项目和等级返回可直接渲染的 JSON 规则内容。
    def test_route_returns_rule_content(self) -> None:
        service_mock = AsyncMock(
            return_value=ProjectRuleContent(
                sub_desc="提升有氧容量和节奏控制",
                rule_content=[
                    {"label": "累计距离", "value": "50km"}
                ],
                rule_note="跑步或快走均可累计",
            )
        )

        with patch(
            "app.router.project.get_project_rule_content_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project/rule",
                    params={"project_id": 2, "level_id": 3},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "sub_desc": "提升有氧容量和节奏控制",
                "rule_content": [
                    {"label": "累计距离", "value": "50km"}
                ],
                "rule_note": "跑步或快走均可累计",
            },
        )
        service_mock.assert_awaited_once_with(self.session, 2, 3)

    # 验证没有对应规则时返回明确的 404 业务提示。
    def test_route_reports_missing_rule(self) -> None:
        with patch(
            "app.router.project.get_project_rule_content_service",
            new=AsyncMock(side_effect=ProjectRuleNotFoundError),
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project/rule",
                    params={"project_id": 2, "level_id": 3},
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {"detail": "未找到对应的项目规则"},
        )

    # 验证项目和等级标识缺失、非整数或非正数时由请求边界拒绝。
    def test_route_validates_identifiers(self) -> None:
        with TestClient(app) as client:
            missing_response = client.get(
                "/flame/admin/api/project/rule"
            )
            invalid_response = client.get(
                "/flame/admin/api/project/rule",
                params={"project_id": 0, "level_id": "invalid"},
            )

        self.assertEqual(missing_response.status_code, 422)
        self.assertEqual(invalid_response.status_code, 422)

    # 验证项目规则接口继承统一管理员认证，未登录时不会查询数据库。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.project.get_project_rule_content_service",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/project/rule",
                    params={"project_id": 2, "level_id": 3},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
