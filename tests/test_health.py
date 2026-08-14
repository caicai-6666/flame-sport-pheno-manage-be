import unittest

from fastapi.testclient import TestClient

from tests.support import configure_test_environment

configure_test_environment()

from app.main import app


class HealthRouteTestCase(unittest.TestCase):
    # 验证基础应用可以启动，并通过 Nginx 转发后的内部路径返回稳定存活状态。
    def test_health_route_returns_service_status(self) -> None:
        with TestClient(app) as client:
            response = client.get("/flame/admin/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["environment"], "development")

    # 验证废弃的版本化路径不再暴露，防止前端继续使用错误的旧地址。
    def test_legacy_api_v1_route_is_not_available(self) -> None:
        with TestClient(app) as client:
            response = client.get("/api/v1/health")

        self.assertEqual(response.status_code, 404)
