import unittest

from tests.support import configure_test_environment

configure_test_environment()

import app.router as router_module
from app.router.dependencies import require_admin_token
from app.router.season import router


class SeasonRouterTestCase(unittest.TestCase):
    # 固定赛季管理路由的独立命名空间，避免后续写接口混入只读统计路由。
    def test_router_uses_season_management_namespace(self) -> None:
        self.assertEqual(router.prefix, "/season")
        self.assertEqual(router.tags, ["season"])
        self.assertIs(router_module.season.router, router)

    # 验证赛季管理路由继承统一管理员认证，后续新增接口无需重复声明依赖。
    def test_router_uses_shared_authentication_dependency(self) -> None:
        dependency_calls = {
            dependency.dependency
            for dependency in router_module.protected_router.dependencies
        }

        self.assertIn(require_admin_token, dependency_calls)
