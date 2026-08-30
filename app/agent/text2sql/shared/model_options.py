"""统一 Text-to-SQL 查询链路的模型请求协议与生成预算。"""

from dataclasses import dataclass
from typing import Any, Final


DEFAULT_ALIGNMENT_MAX_TOKENS: Final[int] = 1_200
DEFAULT_PLANNING_MAX_TOKENS: Final[int] = 3_000
DEFAULT_INSPECTION_MAX_TOKENS: Final[int] = 800
DEFAULT_SQL_MAX_TOKENS: Final[int] = 1_200
DEFAULT_SHAPING_MAX_TOKENS: Final[int] = 1_000
DEFAULT_TRANSLATION_MAX_TOKENS: Final[int] = 1_000
DEFAULT_AUDIT_MAX_TOKENS: Final[int] = 500


@dataclass(frozen=True)
class ModelRequestProfile:
    """描述一种 OpenAI 兼容服务的请求参数差异，不包含业务 Prompt 和工具定义。"""

    name: str

    # 构造关闭服务端隐藏思考的请求参数，输出长度仍由所有请求体系共同限制。
    def build_non_thinking_options(self, max_tokens: int) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True)
class ModelProviderConnection:
    """保存整条查询流水线共享的模型供应商连接，不包含任何业务提示词。"""

    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float


@dataclass(frozen=True)
class DeepSeekRequestProfile(ModelRequestProfile):
    """使用 DeepSeek 官方 Chat Completions 扩展参数关闭思考。"""

    # DeepSeek 要求把 thinking 放在 extra_body，而不是 chat template 参数中。
    def build_non_thinking_options(self, max_tokens: int) -> dict[str, Any]:
        return {
            "extra_body": {"thinking": {"type": "disabled"}},
            "max_tokens": max_tokens,
        }


@dataclass(frozen=True)
class VllmRequestProfile(ModelRequestProfile):
    """使用 vLLM Chat Template 参数关闭支持该开关的模型思考模式。"""

    # vLLM 将 enable_thinking 传给模型聊天模板，模板未声明该变量时由服务端过滤。
    def build_non_thinking_options(self, max_tokens: int) -> dict[str, Any]:
        return {
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": False},
            },
            "max_tokens": max_tokens,
        }


_MODEL_REQUEST_PROFILES: Final[dict[str, ModelRequestProfile]] = {
    "deepseek": DeepSeekRequestProfile(name="deepseek"),
    "vllm": VllmRequestProfile(name="vllm"),
}
SUPPORTED_MODEL_REQUEST_PROFILES: Final[frozenset[str]] = frozenset(
    _MODEL_REQUEST_PROFILES
)


# 根据部署配置选择请求参数策略；未知值在发起模型请求前明确失败。
def get_model_request_profile(profile_name: str) -> ModelRequestProfile:
    normalized_name = profile_name.strip().lower()
    try:
        return _MODEL_REQUEST_PROFILES[normalized_name]
    except KeyError as error:
        supported_names = "、".join(sorted(SUPPORTED_MODEL_REQUEST_PROFILES))
        raise ValueError(
            f"不支持的模型请求体系 `{profile_name}`；允许值为：{supported_names}"
        ) from error


# 根据全局供应商配置解析唯一连接，确保所有查询子图不会混用 DeepSeek 与 vLLM。
def resolve_model_provider_connection(settings: Any) -> ModelProviderConnection:
    provider = settings.agent_query_model_provider
    if provider == "vllm":
        return ModelProviderConnection(
            provider=provider,
            api_key=settings.vllm_api_key.get_secret_value() or "EMPTY",
            base_url=str(settings.vllm_base_url).rstrip("/"),
            model=settings.vllm_model,
            timeout_seconds=settings.vllm_http_timeout_seconds,
        )
    if provider != "deepseek":
        supported_names = "、".join(sorted(SUPPORTED_MODEL_REQUEST_PROFILES))
        raise ValueError(
            f"不支持的查询模型供应商 `{provider}`；允许值为：{supported_names}"
        )
    if settings.deepseek_api_key is None:
        raise RuntimeError("查询智能体选择了 DeepSeek，但未配置 DEEPSEEK_API_KEY")
    return ModelProviderConnection(
        provider=provider,
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=str(settings.deepseek_base_url).rstrip("/"),
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_http_timeout_seconds,
    )


# 按请求体系关闭隐藏思考并限制单次输出，默认值保持现有 DeepSeek 行为兼容。
def build_non_thinking_completion_options(
    max_tokens: int,
    request_profile: str = "deepseek",
) -> dict[str, Any]:
    return get_model_request_profile(request_profile).build_non_thinking_options(
        max_tokens
    )
