"""兼容部分 OpenAI 工具解析器对嵌套对象参数的二次 JSON 序列化。"""

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError


ArgumentsModelT = TypeVar("ArgumentsModelT", bound=BaseModel)


# 递归还原对象或数组形式的嵌套 JSON 字符串，普通文本和非法 JSON 保持原值。
def _decode_embedded_json(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded_value = json.loads(value)
        except json.JSONDecodeError:
            return value
        return _decode_embedded_json(decoded_value)
    if isinstance(value, list):
        return [_decode_embedded_json(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _decode_embedded_json(item)
            for key, item in value.items()
        }
    return value


# 严格校验失败后递归还原嵌套 JSON 字符串，并使用同一 Pydantic 模型重新完整校验。
def validate_tool_arguments_with_embedded_json_fallback(
    arguments_json: str,
    arguments_model: type[ArgumentsModelT],
) -> ArgumentsModelT:
    try:
        return arguments_model.model_validate_json(arguments_json)
    except ValidationError as original_error:
        try:
            arguments_payload = json.loads(arguments_json)
        except (json.JSONDecodeError, TypeError):
            raise original_error
        return arguments_model.model_validate(
            _decode_embedded_json(arguments_payload)
        )
