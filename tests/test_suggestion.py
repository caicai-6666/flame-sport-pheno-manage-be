import unittest
from datetime import datetime
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.suggestions import (
    PendingUserSuggestion,
    fetch_pending_user_suggestions,
)
from app.router.dependencies import require_admin_token
from app.services.suggestions import list_pending_user_suggestions


class FakeMappingsResult:
    # 保存待处理意见的预设映射行，模拟 SQLAlchemy 查询结果。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以支持仓储使用 mappings().all() 调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设意见行并保持数据库排序结果。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存预设查询结果并捕获意见仓储执行的 SQL。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None

    # 模拟单次异步意见查询，避免测试访问开发数据库。
    async def exec(self, statement: object) -> FakeMappingsResult:
        self.statement = statement
        return self.result


class FakeTransactionContext:
    # 初始化可观察的意见查询事务状态。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    # 标记意见查询服务已经进入事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出并保持异常继续传播。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.exited = True
        return False


class FakeServiceSession:
    # 为意见服务测试提供单一可观察事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前意见查询用例的只读事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class SuggestionRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证仓储只筛选可见待处理意见，并保持最近意见优先排序。
    async def test_repository_maps_pending_suggestions(self) -> None:
        created_at = datetime(2026, 8, 12, 9, 30, 0)
        session = FakeRepositorySession(
            [
                {
                    "id": 12,
                    "user_name": "张三",
                    "avatar_url": "/zhang-san.jpg",
                    "content": "希望增加更多户外运动项目",
                    "created_at": created_at,
                },
                {
                    "id": 11,
                    "user_name": "李四",
                    "avatar_url": None,
                    "content": "建议优化排行榜刷新速度",
                    "created_at": datetime(2026, 8, 11, 16, 0, 0),
                },
            ]
        )

        suggestions = await fetch_pending_user_suggestions(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(
            suggestions,
            (
                PendingUserSuggestion(
                    id=12,
                    user_name="张三",
                    avatar_url="/zhang-san.jpg",
                    content="希望增加更多户外运动项目",
                    created_at=created_at,
                ),
                PendingUserSuggestion(
                    id=11,
                    user_name="李四",
                    avatar_url=None,
                    content="建议优化排行榜刷新速度",
                    created_at=datetime(2026, 8, 11, 16, 0, 0),
                ),
            ),
        )
        sql = str(session.statement)
        self.assertIn("FROM user_suggestion", sql)
        self.assertIn("JOIN `user` AS user_account", sql)
        self.assertIn("user_suggestion.status = 1", sql)
        self.assertIn("user_suggestion.processing_stage = 'pending'", sql)
        self.assertIn("user_suggestion.created_at DESC", sql)
        self.assertNotIn("user_account.status", sql)

    # 验证没有可见待处理意见时仓储返回空集合。
    async def test_repository_returns_empty_pending_suggestions(self) -> None:
        session = FakeRepositorySession([])

        suggestions = await fetch_pending_user_suggestions(  # type: ignore[arg-type]
            session
        )

        self.assertEqual(suggestions, ())


class SuggestionServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证待处理意见服务在显式只读事务中调用仓储并返回结果。
    async def test_service_manages_transaction(self) -> None:
        session = FakeServiceSession()
        expected = (
            PendingUserSuggestion(
                id=12,
                user_name="张三",
                avatar_url=None,
                content="希望增加更多户外运动项目",
                created_at=datetime(2026, 8, 12, 9, 30, 0),
            ),
        )
        repository_mock = AsyncMock(return_value=expected)

        with patch(
            "app.services.suggestions.fetch_pending_user_suggestions",
            new=repository_mock,
        ):
            suggestions = await list_pending_user_suggestions(  # type: ignore[arg-type]
                session
            )

        self.assertEqual(suggestions, expected)
        repository_mock.assert_awaited_once_with(session)
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)


class SuggestionRouteTestCase(unittest.TestCase):
    # 为意见路由注入隔离会话并绕过已单独覆盖的认证逻辑。
    def setUp(self) -> None:
        self.session = object()

        # 返回意见路由测试专用会话，真实查询由服务替身隔离。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止意见接口测试污染其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口完整序列化待处理意见正文、用户展示信息和创建时间。
    def test_route_returns_pending_suggestions(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                PendingUserSuggestion(
                    id=12,
                    user_name="张三",
                    avatar_url="/zhang-san.jpg",
                    content="希望增加更多户外运动项目",
                    created_at=datetime(2026, 8, 12, 9, 30, 0),
                ),
            )
        )

        with patch(
            "app.router.suggestion.list_pending_user_suggestions",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/suggestion/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": 12,
                    "user_name": "张三",
                    "avatar_url": "/zhang-san.jpg",
                    "content": "希望增加更多户外运动项目",
                    "created_at": "2026-08-12T09:30:00",
                }
            ],
        )
        service_mock.assert_awaited_once_with(self.session)

    # 验证没有可见待处理意见时接口返回成功空数组。
    def test_route_returns_empty_array(self) -> None:
        with patch(
            "app.router.suggestion.list_pending_user_suggestions",
            new=AsyncMock(return_value=()),
        ):
            with TestClient(app) as client:
                response = client.get("/flame/admin/api/suggestion/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    # 验证意见接口继承统一管理员认证，未登录时不执行查询服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.suggestion.list_pending_user_suggestions",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/suggestion/list",
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
