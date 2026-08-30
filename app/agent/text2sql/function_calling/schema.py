"""将子图本地 Pydantic 参数模型转换为标准 OpenAI Function Calling 定义。"""

from __future__ import annotations

from pydantic import BaseModel


# 使用模型类说明描述工具用途，并使用 Field.description 描述每一个函数参数。
def build_pydantic_tool_definition(
    tool_name: str,
    arguments_model: type[BaseModel],
    description: str | None = None,
) -> dict[str, object]:
    tool_description = description or (arguments_model.__doc__ or "").strip()
    if not tool_description:
        raise ValueError(f"工具 `{tool_name}` 的 Pydantic 参数模型必须提供类说明")
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": tool_description,
            "parameters": arguments_model.model_json_schema(mode="validation"),
        },
    }
