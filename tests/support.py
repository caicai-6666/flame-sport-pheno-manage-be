"""提供不依赖本机敏感配置的测试环境初始化。"""

import os

TEST_ADMIN_KEY = "test-admin-key-with-at-least-32-characters"


# 在导入应用配置前注入公开测试密钥，确保测试不会读取或泄露开发环境密钥。
def configure_test_environment() -> None:
    os.environ.setdefault("ADMIN_KEY", TEST_ADMIN_KEY)
