import unittest
from typing import get_args
from unittest.mock import patch

from fastapi.params import Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import DatabaseSession, get_session


class FakeSessionContext:
    # 为单次依赖调用记录会话是否已关闭，且不隐式管理业务事务。
    def __init__(self) -> None:
        self.closed = False

    # 模拟异步会话进入请求作用域，并返回可注入的会话对象。
    async def __aenter__(self) -> "FakeSessionContext":
        return self

    # 模拟请求完成后的会话关闭，确保异常继续向上传播给接口层处理。
    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> bool:
        self.closed = True
        return False


class DatabaseSessionDependencyTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证依赖仅在被消费时创建会话，并在正常完成后关闭会话而不干预事务。
    async def test_session_yields_and_closes_after_success(self) -> None:
        fake_session = FakeSessionContext()

        with patch(
            "app.db.session.async_session_factory",
            return_value=fake_session,
        ):
            sessions = [session async for session in get_session()]

        self.assertEqual(sessions, [fake_session])
        self.assertTrue(fake_session.closed)

    # 验证接口内部异常会原样传播，同时依赖仍可靠关闭请求级会话。
    async def test_session_closes_and_preserves_error(self) -> None:
        fake_session = FakeSessionContext()

        with patch(
            "app.db.session.async_session_factory",
            return_value=fake_session,
        ):
            session_generator = get_session()
            yielded_session = await anext(session_generator)
            with self.assertRaisesRegex(RuntimeError, "database operation failed"):
                await session_generator.athrow(
                    RuntimeError("database operation failed")
                )

        self.assertIs(yielded_session, fake_session)
        self.assertTrue(fake_session.closed)

    # 验证类型别名将 SQLModel 异步会话与统一会话依赖绑定，供接口直接声明。
    def test_database_session_alias_uses_session_dependency(self) -> None:
        session_type, dependency = get_args(DatabaseSession)

        self.assertIs(session_type, AsyncSession)
        self.assertIsInstance(dependency, Depends)
        self.assertIs(dependency.dependency, get_session)
