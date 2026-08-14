import unittest
from types import TracebackType
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.repositories.suggestions import (
    SuggestionForProcessing,
    fetch_suggestion_for_processing,
    update_suggestion_processing_stage,
)
from app.router.dependencies import require_admin_token
from app.services.suggestions import (
    SuggestionNotFoundError,
    SuggestionProcessingConflictError,
    SuggestionProcessingResult,
    process_user_suggestion,
)


class FakeMappingsResult:
    # 保存意见处理查询的唯一预设行，以模拟 SQLAlchemy 映射结果。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    # 返回自身以支持仓储使用 mappings().first()。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回预设意见行，空值表示目标记录不存在。
    def first(self) -> dict[str, object] | None:
        return self.row


class FakeRepositorySession:
    # 保存意见处理查询结果，并记录仓储执行的 SQL 与参数。
    def __init__(self, row: dict[str, object] | None) -> None:
        self.result = FakeMappingsResult(row)
        self.statement = None
        self.params = None

    # 模拟参数化异步查询，避免访问开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class FakeTransactionContext:
    # 初始化可观察事务，并记录异常退出类型以验证回滚路径。
    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.exception_type: type[BaseException] | None = None

    # 标记意见处理用例已经进入事务。
    async def __aenter__(self) -> "FakeTransactionContext":
        self.entered = True
        return self

    # 标记事务退出且不吞掉异常，以模拟真实提交或回滚行为。
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
    # 为意见处理服务测试提供独立的可观察事务。
    def __init__(self) -> None:
        self.transaction = FakeTransactionContext()

    # 返回当前处理用例唯一的事务上下文。
    def begin(self) -> FakeTransactionContext:
        return self.transaction


class SuggestionProcessingRepositoryTestCase(
    unittest.IsolatedAsyncioTestCase
):
    # 验证仓储使用主键参数和行锁读取意见状态，支持并发安全处理。
    async def test_repository_locks_suggestion_for_processing(self) -> None:
        session = FakeRepositorySession(
            {
                "id": 12,
                "status": 1,
                "processing_stage": "pending",
            }
        )

        suggestion = await fetch_suggestion_for_processing(  # type: ignore[arg-type]
            session,
            suggestion_id=12,
        )

        self.assertEqual(
            suggestion,
            SuggestionForProcessing(
                id=12,
                status=1,
                processing_stage="pending",
            ),
        )
        self.assertEqual(session.params, {"suggestion_id": 12})
        self.assertIn(
            "WHERE user_suggestion.id = :suggestion_id",
            str(session.statement),
        )
        self.assertIn("FOR UPDATE", str(session.statement))

    # 验证处理阶段写入使用固定参数绑定，避免意见 ID 或状态进入 SQL 文本。
    async def test_repository_updates_processing_stage(self) -> None:
        session = FakeRepositorySession(None)

        await update_suggestion_processing_stage(  # type: ignore[arg-type]
            session,
            suggestion_id=12,
            processing_stage="optimized",
        )

        self.assertEqual(
            session.params,
            {
                "suggestion_id": 12,
                "processing_stage": "optimized",
            },
        )
        self.assertIn(
            "SET processing_stage = :processing_stage",
            str(session.statement),
        )


class SuggestionProcessingServiceTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证已解决动作映射为数据库 optimized，并在单一事务中写入。
    async def test_resolved_action_updates_optimized_stage(self) -> None:
        session = FakeServiceSession()
        update_mock = AsyncMock()

        with (
            patch(
                "app.services.suggestions.fetch_suggestion_for_processing",
                new=AsyncMock(
                    return_value=SuggestionForProcessing(12, 1, "pending")
                ),
            ),
            patch(
                "app.services.suggestions.update_suggestion_processing_stage",
                new=update_mock,
            ),
        ):
            result = await process_user_suggestion(  # type: ignore[arg-type]
                session,
                suggestion_id=12,
                action="resolved",
            )

        self.assertEqual(
            result,
            SuggestionProcessingResult(12, "resolved"),
        )
        update_mock.assert_awaited_once_with(session, 12, "optimized")
        self.assertTrue(session.transaction.entered)
        self.assertTrue(session.transaction.exited)

    # 验证重复提交相同动作按幂等成功返回，不重复执行数据库更新。
    async def test_same_action_is_idempotent(self) -> None:
        session = FakeServiceSession()
        update_mock = AsyncMock()

        with (
            patch(
                "app.services.suggestions.fetch_suggestion_for_processing",
                new=AsyncMock(
                    return_value=SuggestionForProcessing(12, 1, "rejected")
                ),
            ),
            patch(
                "app.services.suggestions.update_suggestion_processing_stage",
                new=update_mock,
            ),
        ):
            result = await process_user_suggestion(  # type: ignore[arg-type]
                session,
                suggestion_id=12,
                action="rejected",
            )

        self.assertEqual(result.processing_stage, "rejected")
        update_mock.assert_not_awaited()

    # 验证意见已有不同终态时拒绝覆盖，并通过异常退出事务。
    async def test_different_final_action_conflicts(self) -> None:
        session = FakeServiceSession()

        with patch(
            "app.services.suggestions.fetch_suggestion_for_processing",
            new=AsyncMock(
                return_value=SuggestionForProcessing(12, 1, "rejected")
            ),
        ):
            with self.assertRaises(SuggestionProcessingConflictError):
                await process_user_suggestion(  # type: ignore[arg-type]
                    session,
                    suggestion_id=12,
                    action="resolved",
                )

        self.assertIs(
            session.transaction.exception_type,
            SuggestionProcessingConflictError,
        )

    # 验证不存在或已隐藏的意见不会进入处理更新。
    async def test_missing_or_hidden_suggestion_is_not_found(self) -> None:
        for suggestion in (None, SuggestionForProcessing(12, 0, "pending")):
            with self.subTest(suggestion=suggestion):
                session = FakeServiceSession()
                with patch(
                    "app.services.suggestions.fetch_suggestion_for_processing",
                    new=AsyncMock(return_value=suggestion),
                ):
                    with self.assertRaises(SuggestionNotFoundError):
                        await process_user_suggestion(  # type: ignore[arg-type]
                            session,
                            suggestion_id=12,
                            action="rejected",
                        )


class SuggestionProcessingRouteTestCase(unittest.TestCase):
    # 为意见处理路由注入隔离会话，并绕过已单独覆盖的认证逻辑。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前意见处理接口测试专用的会话替身。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，防止处理接口测试影响其他路由。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口接收意见 ID 与拒绝动作并返回最终处理状态。
    def test_route_processes_suggestion(self) -> None:
        service_mock = AsyncMock(
            return_value=SuggestionProcessingResult(12, "rejected")
        )

        with patch(
            "app.router.suggestion.process_user_suggestion",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/suggestion/process",
                    json={"suggestion_id": 12, "action": "rejected"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"suggestion_id": 12, "processing_stage": "rejected"},
        )
        service_mock.assert_awaited_once_with(self.session, 12, "rejected")

    # 验证意见不存在和处理结论冲突具有不同的 HTTP 错误语义。
    def test_route_maps_processing_errors(self) -> None:
        cases = (
            (SuggestionNotFoundError(), 404, "意见不存在或已隐藏"),
            (
                SuggestionProcessingConflictError(),
                409,
                "意见已有不同处理结论，不能重复处理",
            ),
        )
        for exception, expected_status, expected_detail in cases:
            with self.subTest(exception=type(exception).__name__):
                with patch(
                    "app.router.suggestion.process_user_suggestion",
                    new=AsyncMock(side_effect=exception),
                ):
                    with TestClient(app) as client:
                        response = client.post(
                            "/flame/admin/api/suggestion/process",
                            json={
                                "suggestion_id": 12,
                                "action": "resolved",
                            },
                        )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )

    # 验证意见 ID 和处理动作不合法时由请求边界拒绝。
    def test_route_validates_processing_request(self) -> None:
        with TestClient(app) as client:
            invalid_id_response = client.post(
                "/flame/admin/api/suggestion/process",
                json={"suggestion_id": 0, "action": "rejected"},
            )
            invalid_action_response = client.post(
                "/flame/admin/api/suggestion/process",
                json={"suggestion_id": 12, "action": "pending"},
            )

        self.assertEqual(invalid_id_response.status_code, 422)
        self.assertEqual(invalid_action_response.status_code, 422)

    # 验证意见处理接口继承管理员认证，未登录时不执行写服务。
    def test_route_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.suggestion.process_user_suggestion",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/flame/admin/api/suggestion/process",
                    json={"suggestion_id": 12, "action": "resolved"},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()
