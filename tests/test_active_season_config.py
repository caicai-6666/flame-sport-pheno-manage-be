import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from tests.support import TEST_ADMIN_KEY, configure_test_environment

configure_test_environment()

from app.core.config import Settings


class ActiveSeasonConfigurationTestCase(unittest.TestCase):
    # 验证激活赛季配置变更窗口可以通过环境配置覆盖，并保留零小时的立即锁定语义。
    def test_edit_window_hours_is_configurable(self) -> None:
        with patch.dict(
            os.environ,
            {"ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS": "0"},
        ):
            settings = Settings(admin_key=TEST_ADMIN_KEY)

        self.assertEqual(settings.active_season_config_edit_window_hours, 0)

    # 验证负数窗口在应用启动阶段被拒绝，避免后续写接口产生相反的时间判断。
    def test_negative_edit_window_hours_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS": "-1"},
        ):
            with self.assertRaises(ValidationError):
                Settings(admin_key=TEST_ADMIN_KEY)
