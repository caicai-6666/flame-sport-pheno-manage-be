"""统一正式查询链路的 DeepSeek 客户端选项与生成预算。"""

from typing import Any, Final


DEFAULT_ALIGNMENT_MAX_TOKENS: Final[int] = 1_200
DEFAULT_PLANNING_MAX_TOKENS: Final[int] = 3_000
DEFAULT_INSPECTION_MAX_TOKENS: Final[int] = 800
DEFAULT_SQL_MAX_TOKENS: Final[int] = 1_200
DEFAULT_TRANSLATION_MAX_TOKENS: Final[int] = 1_000
DEFAULT_AUDIT_MAX_TOKENS: Final[int] = 500


# 构造关闭隐藏思考并限制单次输出长度的请求参数，避免工具型循环出现不可控输出成本。
def build_non_thinking_completion_options(max_tokens: int) -> dict[str, Any]:
    return {
        "extra_body": {"thinking": {"type": "disabled"}},
        "max_tokens": max_tokens,
    }


# 将兼容 API 基础地址切换到 DeepSeek strict function calling 所要求的 Beta 路径。
def build_strict_tools_base_url(base_url: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    if normalized_base_url.endswith("/beta"):
        return normalized_base_url
    return f"{normalized_base_url}/beta"
