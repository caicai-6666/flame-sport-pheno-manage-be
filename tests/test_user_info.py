import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.db.session import get_session
from app.main import app
from app.router.dependencies import require_admin_token
from app.repositories.users import (
    UserBasicInformation,
    fetch_user_basic_information,
)
from app.services.users import deduplicate_user_ids


class FakeMappingsResult:
    # 保存仓储测试预设的数据库映射行。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    # 返回自身以模拟 SQLAlchemy 的映射结果调用链。
    def mappings(self) -> "FakeMappingsResult":
        return self

    # 返回全部预设映射行。
    def all(self) -> list[dict[str, object]]:
        return self.rows


class FakeRepositorySession:
    # 保存查询结果，并捕获仓储传入的 SQL 和参数。
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.result = FakeMappingsResult(rows)
        self.statement = None
        self.params = None

    # 模拟一次异步批量查询，避免测试依赖开发数据库。
    async def exec(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> FakeMappingsResult:
        self.statement = statement
        self.params = params
        return self.result


class UserInformationRepositoryTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证批量查询联动部门信息，并按照请求 ID 顺序映射已有用户。
    async def test_repository_maps_users_in_requested_order(self) -> None:
        session = FakeRepositorySession(
            [
                {
                    "user_id": "user-1",
                    "name": "张三",
                    "department_name": "研发部",
                    "avatar_url": "/avatar/user-1.jpg",
                },
                {
                    "user_id": "user-2",
                    "name": "李四",
                    "department_name": "产品部",
                    "avatar_url": None,
                },
            ]
        )

        users = await fetch_user_basic_information(  # type: ignore[arg-type]
            session,
            ("user-2", "missing-user", "user-1"),
        )

        self.assertEqual(
            users,
            (
                UserBasicInformation(
                    user_id="user-2",
                    name="李四",
                    department_name="产品部",
                    avatar_url=None,
                ),
                UserBasicInformation(
                    user_id="user-1",
                    name="张三",
                    department_name="研发部",
                    avatar_url="/avatar/user-1.jpg",
                ),
            ),
        )
        self.assertEqual(
            session.params,
            {"user_ids": ("user-2", "missing-user", "user-1")},
        )
        self.assertIn("JOIN department", str(session.statement))
        self.assertIn("user_account.id IN", str(session.statement))

    # 验证空 ID 集合不会发起无意义数据库查询。
    async def test_repository_skips_query_for_empty_ids(self) -> None:
        session = FakeRepositorySession([])

        users = await fetch_user_basic_information(session, ())  # type: ignore[arg-type]

        self.assertEqual(users, ())
        self.assertIsNone(session.statement)


class UserInformationRouteTestCase(unittest.TestCase):
    # 为路由测试绕过认证并注入不访问真实数据库的会话。
    def setUp(self) -> None:
        self.session = object()

        # 返回当前路由测试专用会话。
        async def override_session():
            yield self.session

        app.dependency_overrides[require_admin_token] = lambda: None
        app.dependency_overrides[get_session] = override_session

    # 清理依赖覆盖，避免影响其他测试模块。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证接口序列化服务层结果，并保留可空头像字段。
    def test_user_info_returns_basic_information(self) -> None:
        service_mock = AsyncMock(
            return_value=(
                UserBasicInformation(
                    user_id="user-2",
                    name="李四",
                    department_name="产品部",
                    avatar_url=None,
                ),
                UserBasicInformation(
                    user_id="user-1",
                    name="张三",
                    department_name="研发部",
                    avatar_url="/avatar/user-1.jpg",
                ),
            )
        )

        with patch(
            "app.router.user.get_user_basic_information",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/user/user-info",
                    params=[
                        ("user_ids", "user-2"),
                        ("user_ids", "user-1"),
                        ("user_ids", "user-2"),
                    ],
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "user_id": "user-2",
                    "name": "李四",
                    "department_name": "产品部",
                    "avatar_url": None,
                },
                {
                    "user_id": "user-1",
                    "name": "张三",
                    "department_name": "研发部",
                    "avatar_url": "/avatar/user-1.jpg",
                },
            ],
        )
        service_mock.assert_awaited_once_with(
            self.session,
            ["user-2", "user-1", "user-2"],
        )

    # 验证缺少用户 ID 时由请求边界返回参数校验错误。
    def test_user_info_requires_at_least_one_id(self) -> None:
        with TestClient(app) as client:
            response = client.get("/flame/admin/api/user/user-info")

        self.assertEqual(response.status_code, 422)

    # 验证超过批量上限时拒绝请求，避免过长 URL 和无界数据库查询。
    def test_user_info_rejects_more_than_fifty_ids(self) -> None:
        with TestClient(app) as client:
            response = client.get(
                "/flame/admin/api/user/user-info",
                params=[("user_ids", f"user-{index}") for index in range(51)],
            )

        self.assertEqual(response.status_code, 422)

    # 验证用户信息接口继承受保护路由的统一管理员认证依赖。
    def test_user_info_requires_admin_token(self) -> None:
        app.dependency_overrides.pop(require_admin_token)
        service_mock = AsyncMock()

        with patch(
            "app.router.user.get_user_basic_information",
            new=service_mock,
        ):
            with TestClient(app) as client:
                response = client.get(
                    "/flame/admin/api/user/user-info",
                    params={"user_ids": "user-1"},
                    follow_redirects=False,
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        service_mock.assert_not_awaited()


class UserIdentifierDeduplicationTestCase(unittest.TestCase):
    # 验证用户 ID 去重保留首次出现位置，保证响应顺序稳定。
    def test_deduplicate_user_ids_preserves_first_occurrence(self) -> None:
        self.assertEqual(
            deduplicate_user_ids(["user-2", "user-1", "user-2"]),
            ("user-2", "user-1"),
        )
