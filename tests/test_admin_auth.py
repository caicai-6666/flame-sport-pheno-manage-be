import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.support import TEST_ADMIN_KEY, configure_test_environment

configure_test_environment()

from app.router.dependencies import get_admin_token_cache
from app.core.admin_auth import AdminTokenCache
from app.core.config import Settings
from app.main import app


class AdminTokenCacheTestCase(unittest.IsolatedAsyncioTestCase):
    # 验证正确密钥能够换取令牌，并且缓存可以识别该令牌而拒绝伪造值。
    async def test_issue_and_validate_token(self) -> None:
        token_cache = AdminTokenCache(
            admin_key=TEST_ADMIN_KEY,
            ttl_seconds=60,
            max_size=10,
        )

        token = await token_cache.issue(TEST_ADMIN_KEY)

        self.assertIsNotNone(token)
        self.assertTrue(await token_cache.validate(token or ""))
        self.assertFalse(await token_cache.validate("invalid-token"))

    # 验证密钥错误时不签发令牌，避免无效尝试占用缓存容量。
    async def test_invalid_admin_key_does_not_issue_token(self) -> None:
        token_cache = AdminTokenCache(
            admin_key=TEST_ADMIN_KEY,
            ttl_seconds=60,
            max_size=10,
        )

        token = await token_cache.issue("incorrect-admin-key")

        self.assertIsNone(token)

    # 使用可控时钟验证令牌到期后立即失效，不依赖真实等待时间。
    async def test_expired_token_is_rejected(self) -> None:
        current_time = [100.0]
        token_cache = AdminTokenCache(
            admin_key=TEST_ADMIN_KEY,
            ttl_seconds=60,
            max_size=10,
            clock=lambda: current_time[0],
        )
        token = await token_cache.issue(TEST_ADMIN_KEY)
        current_time[0] = 160.0

        self.assertFalse(await token_cache.validate(token or ""))

    # 验证缓存达到容量上限时淘汰最早到期令牌，避免令牌数量无界增长。
    async def test_cache_evicts_earliest_expiring_token(self) -> None:
        current_time = [100.0]
        token_cache = AdminTokenCache(
            admin_key=TEST_ADMIN_KEY,
            ttl_seconds=60,
            max_size=1,
            clock=lambda: current_time[0],
        )
        first_token = await token_cache.issue(TEST_ADMIN_KEY)
        current_time[0] = 101.0
        second_token = await token_cache.issue(TEST_ADMIN_KEY)

        self.assertFalse(await token_cache.validate(first_token or ""))
        self.assertTrue(await token_cache.validate(second_token or ""))


class AdminKeyConfigurationTestCase(unittest.TestCase):
    # 验证管理员密钥只要求为非空密码，不再限制至少 32 个字符。
    def test_short_non_empty_admin_key_is_allowed(self) -> None:
        settings = Settings(admin_key="123456")

        self.assertEqual(settings.admin_key.get_secret_value(), "123456")

    # 验证空密码仍会阻止配置加载，避免服务意外退化为无密码认证。
    def test_empty_admin_key_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(admin_key="")


class AdminAuthenticationRouteTestCase(unittest.TestCase):
    # 为每个接口测试提供隔离令牌缓存，避免依赖本机真实管理员密钥。
    def setUp(self) -> None:
        self.token_cache = AdminTokenCache(
            admin_key=TEST_ADMIN_KEY,
            ttl_seconds=300,
            max_size=10,
        )
        app.dependency_overrides[get_admin_token_cache] = (
            lambda: self.token_cache
        )

    # 清理依赖覆盖，防止认证测试状态影响其他接口测试。
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    # 验证前端可通过登录接口提交正确密钥，并使用返回的 Bearer Token 校验会话。
    def test_admin_login_returns_bearer_token(self) -> None:
        with TestClient(app) as client:
            login_response = client.post(
                "/flame/admin/api/auth/login",
                json={"admin_key": TEST_ADMIN_KEY},
            )
            access_token = login_response.json()["access_token"]
            session_response = client.get(
                "/flame/admin/api/auth/session",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["token_type"], "bearer")
        self.assertEqual(login_response.json()["expires_in"], 300)
        self.assertEqual(session_response.status_code, 200)
        self.assertEqual(session_response.json(), {"authenticated": True})

    # 验证错误管理员密钥统一返回未认证，且响应中不会回显密钥。
    def test_invalid_admin_key_is_rejected(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/flame/admin/api/auth/login",
                json={"admin_key": "incorrect-admin-key"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("incorrect-admin-key", response.text)

    # 验证统一依赖将缺失或未命中缓存的 token 重定向至登录接口。
    def test_session_redirects_missing_and_invalid_tokens_to_login(self) -> None:
        with TestClient(app) as client:
            missing_response = client.get(
                "/flame/admin/api/auth/session",
                follow_redirects=False,
            )
            invalid_response = client.get(
                "/flame/admin/api/auth/session",
                headers={"Authorization": "Bearer invalid-token"},
                follow_redirects=False,
            )

        self.assertEqual(missing_response.status_code, 303)
        self.assertEqual(invalid_response.status_code, 303)
        self.assertEqual(
            missing_response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )
        self.assertEqual(
            invalid_response.headers["location"],
            "/dev/flame/admin/api/auth/login",
        )

    # 验证旧 token 路径不再可用，避免前端继续依赖已废弃的接口名称。
    def test_legacy_token_route_is_not_available(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/flame/admin/api/auth/token",
                json={"admin_key": TEST_ADMIN_KEY},
            )

        self.assertEqual(response.status_code, 404)
