"""将本地 Pydantic 工具参数模型适配为 DeepSeek strict function calling Schema。"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel


_UNSUPPORTED_STRICT_KEYWORDS = frozenset(
    {
        "default",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "title",
    }
)


class StrictToolSchemaValidationError(ValueError):
    """工具 JSON Schema 不满足 DeepSeek strict 封闭对象约束。"""


# 将 Pydantic 标准 JSON Schema 的定义容器与引用改写为 DeepSeek strict 方言的 $def 路径。
def _rewrite_deepseek_definition_reference(value: str) -> str:
    return value.replace("#/$defs/", "#/$def/")


# 递归移除 strict Schema 不支持的展示与校验关键词，并补齐所有对象字段的必填和封闭约束。
def _normalize_strict_schema_node(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalize_strict_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    normalized_node: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_STRICT_KEYWORDS:
            continue
        normalized_key = "$def" if key == "$defs" else key
        if key == "$ref" and isinstance(value, str):
            normalized_node[normalized_key] = _rewrite_deepseek_definition_reference(
                value
            )
            continue
        normalized_node[normalized_key] = _normalize_strict_schema_node(value)
    properties = normalized_node.get("properties")
    if isinstance(properties, dict):
        normalized_node["required"] = list(properties)
        normalized_node["additionalProperties"] = False
    return normalized_node


# 递归检查所有 object 节点均声明固定属性、全部必填且禁止额外字段，避免无效请求到达模型服务。
def validate_deepseek_strict_schema(
    schema: dict[str, Any],
    path: str = "$",
) -> None:
    if schema.get("type") == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise StrictToolSchemaValidationError(
                f"strict Schema 的对象 `{path}` 必须使用 properties 声明固定字段，"
                "不能使用动态键对象"
            )
        if schema.get("additionalProperties") is not False:
            raise StrictToolSchemaValidationError(
                f"strict Schema 的对象 `{path}` 必须设置 additionalProperties=false"
            )
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            raise StrictToolSchemaValidationError(
                f"strict Schema 的对象 `{path}` 必须把全部 properties 字段列入 required"
            )

    for key, value in schema.items():
        child_path = f"{path}.{key}"
        if isinstance(value, dict):
            validate_deepseek_strict_schema(value, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    validate_deepseek_strict_schema(
                        item,
                        f"{child_path}[{index}]",
                    )


# 从 Pydantic 模型生成仅供远端 strict 校验使用的参数 Schema，本地约束仍由原模型继续校验。
def build_strict_parameters_schema(arguments_model: type[BaseModel]) -> dict[str, Any]:
    parameters_schema = _normalize_strict_schema_node(
        deepcopy(arguments_model.model_json_schema())
    )
    validate_deepseek_strict_schema(parameters_schema)
    return parameters_schema


# 构造 DeepSeek Beta strict function calling 定义，使服务端保证工具 arguments 始终是有效 JSON。
def build_strict_tool_definition(
    tool_name: str,
    description: str,
    arguments_model: type[BaseModel],
) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": description,
            "strict": True,
            "parameters": build_strict_parameters_schema(arguments_model),
        },
    }
