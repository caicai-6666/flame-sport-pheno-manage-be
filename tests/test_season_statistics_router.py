import unittest

from tests.support import configure_test_environment

configure_test_environment()

import app.router as router_module
from app.router.dependencies import require_admin_token
from app.router.season_statistics import router


class SeasonStatisticsRouterTestCase(unittest.TestCase):
    # 固定赛季统计子路由的路径与标签，避免后续聚合接口散落到其他业务命名空间。
    def test_router_uses_dedicated_namespace(self) -> None:
        self.assertEqual(router.prefix, "/season-statistics")
        self.assertEqual(router.tags, ["season-statistics"])
        self.assertIs(router_module.season_statistics.router, router)

    # 验证所有业务子路由统一挂载在同一个管理员 token 缓存校验依赖下。
    def test_business_router_uses_shared_authentication_dependency(self) -> None:
        dependency_calls = {
            dependency.dependency
            for dependency in router_module.protected_router.dependencies
        }

        self.assertIn(require_admin_token, dependency_calls)
