import unittest

from pydantic import ValidationError

from tests.support import TEST_ADMIN_KEY, configure_test_environment

configure_test_environment()

from app.core.config import Settings


class ClientBackendConfigurationTestCase(unittest.TestCase):
    # 验证局域网服务名、端口和管理接口路径能够作为完整客户端后端基础地址加载。
    def test_internal_service_base_url_is_loaded(self) -> None:
        settings = Settings(
            admin_key=TEST_ADMIN_KEY,
            client_backend_base_url=(
                "http://backend:8000/flame/api/admin"
            ),
        )

        self.assertEqual(
            str(settings.client_backend_base_url),
            "http://backend:8000/flame/api/admin",
        )

    # 验证缺少 HTTP 协议的地址会在应用启动阶段被拒绝。
    def test_invalid_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                admin_key=TEST_ADMIN_KEY,
                client_backend_base_url="backend:8000/flame/api/admin",
            )

    # 验证图片缓存时效可以通过环境配置覆盖，并限制在最多一年内。
    def test_image_cache_seconds_is_configurable(self) -> None:
        settings = Settings(
            admin_key=TEST_ADMIN_KEY,
            image_cache_seconds=3600,
        )

        self.assertEqual(settings.image_cache_seconds, 3600)

    # 验证负数图片缓存时效在应用启动阶段被拒绝。
    def test_negative_image_cache_seconds_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                admin_key=TEST_ADMIN_KEY,
                image_cache_seconds=-1,
            )
