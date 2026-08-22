from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "燃动现象管理端后端"
    app_env: Literal["development", "testing", "staging", "production"] = (
        "development"
    )
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8001, ge=1, le=65535)
    api_prefix: str = "/flame/admin/api"
    public_api_prefix: str = "/dev/flame/admin/api"
    cors_origins: list[str] = Field(default_factory=list)

    admin_key: SecretStr = Field(min_length=1)
    admin_token_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    admin_token_cache_max_size: int = Field(default=1000, ge=1, le=10000)

    # 统一限制激活赛季开始后仍可调整高影响业务配置的小时数，零表示不保留修改窗口。
    active_season_config_edit_window_hours: int = Field(default=24, ge=0)

    # 控制赛季状态后台检查；默认启用，并限制轮询间隔以避免异常配置造成数据库忙轮询。
    season_status_check_enabled: bool = True
    season_status_check_interval_seconds: int = Field(
        default=60,
        ge=1,
        le=86400,
    )
    season_settlement_review_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
    )
    season_settlement_review_concurrency: int = Field(
        default=5,
        ge=1,
        le=50,
    )
    season_settlement_user_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
    )
    # 自动一键结算默认关闭；启用后按上海业务日期等待配置天数再强制收口。
    season_settlement_auto_complete_enabled: bool = False
    season_settlement_auto_complete_after_days: int = Field(
        default=7,
        ge=0,
        le=365,
    )
    # 连续完整达成赛季的奖励允许按部署环境调整，默认值保持现行业务规则。
    season_settlement_two_month_streak_bonus_points: int = Field(
        default=50,
        ge=0,
        le=4_294_967_295,
    )
    season_settlement_three_month_streak_bonus_points: int = Field(
        default=100,
        ge=0,
        le=4_294_967_295,
    )

    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = "root"
    mysql_password: SecretStr = SecretStr("")
    mysql_database: str = "flame_sport_pheno"
    mysql_charset: str = "utf8mb4"
    mysql_echo: bool = False
    mysql_pool_size: int = Field(default=10, ge=1)
    mysql_max_overflow: int = Field(default=20, ge=0)
    mysql_pool_recycle_seconds: int = Field(default=1800, ge=1)

    client_backend_base_url: AnyHttpUrl = AnyHttpUrl(
        "http://backend:8000/flame/api/admin"
    )
    client_backend_timeout_seconds: float = Field(default=10.0, gt=0)
    image_cache_seconds: int = Field(default=86400, ge=0, le=31536000)

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: AnyHttpUrl = AnyHttpUrl("https://api.deepseek.com")
    deepseek_model: str = Field(default="deepseek-v4-flash", min_length=1)
    deepseek_http_timeout_seconds: float = Field(default=60.0, gt=0)
    # 查询智能体为不同职责设定单次生成硬上限，防止工具循环或 JSON 输出意外放大成本。
    deepseek_query_alignment_max_tokens: int = Field(default=1200, ge=128, le=8192)
    deepseek_query_planning_max_tokens: int = Field(default=3000, ge=256, le=8192)
    deepseek_query_inspection_max_tokens: int = Field(default=800, ge=128, le=4096)
    deepseek_query_sql_max_tokens: int = Field(default=1200, ge=128, le=4096)
    deepseek_query_translation_max_tokens: int = Field(
        default=1000,
        ge=128,
        le=4096,
    )
    deepseek_query_audit_max_tokens: int = Field(default=500, ge=128, le=2048)

    # 分阶段限制正式查询智能体的模型轮次和工具调用，避免单次请求无限消耗资源。
    agent_query_alignment_max_generations: int = Field(default=10, ge=1, le=30)
    agent_query_planning_max_generations: int = Field(default=30, ge=1, le=60)
    agent_query_planning_max_tool_calls: int = Field(default=30, ge=1, le=100)
    agent_query_sql_max_generations: int = Field(default=4, ge=1, le=10)
    agent_query_translation_max_parallel_fields: int = Field(
        default=4,
        ge=1,
        le=16,
    )
    # 查询会话暂存于单进程内存；容量、事件历史和过期时间共同限制资源占用。
    agent_query_max_active_sessions: int = Field(default=20, ge=1, le=200)
    agent_query_event_history_size: int = Field(default=200, ge=20, le=2000)
    agent_query_session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    agent_query_sse_heartbeat_seconds: int = Field(default=15, ge=5, le=60)

    # 使用 SQLAlchemy URL 安全拼装异步 MySQL 地址，避免密码中的特殊字符破坏连接串。
    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="mysql+asyncmy",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": self.mysql_charset},
        )


# 缓存完成校验的配置对象，保证各模块读取到同一份进程级配置。
@lru_cache
def get_settings() -> Settings:
    return Settings()
