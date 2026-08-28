"""将工具参数校验异常转换为可供 Text-to-SQL 模型修复的标准返回值。"""

from collections.abc import Sequence
import json
import re
from typing import Any, Final

from pydantic import ValidationError


TOOL_ARGUMENT_VALIDATION_ERROR_CODE: Final[str] = (
    "tool_arguments_validation_failed"
)


# 将 Pydantic 字段定位转换为模型容易理解的点分路径，顶层错误统一使用 `$`。
def _format_error_path(location: tuple[int | str, ...]) -> str:
    if not location:
        return "$"
    return ".".join(str(part) for part in location)


# 根据 Pydantic 错误代码返回稳定的错误类别，避免模型依赖内部错误文案进行修复。
def _classify_validation_error(error_type: str) -> str:
    if error_type == "json_invalid":
        return "invalid_json"
    if error_type == "missing":
        return "missing_required_field"
    if error_type == "extra_forbidden":
        return "extra_field"
    if error_type in {"literal_error", "enum"}:
        return "invalid_enum_value"
    if error_type.endswith(("_type", "_parsing")) or error_type in {
        "model_attributes_type",
        "json_type",
        "int_from_float",
        "none_required",
    }:
        return "type_mismatch"
    if error_type.startswith(
        (
            "greater_than",
            "less_than",
            "too_short",
            "too_long",
            "string_too_",
            "multiple_of",
            "finite_number",
        )
    ):
        return "constraint_violation"
    if error_type == "value_error":
        return "business_rule_violation"
    return "invalid_argument"


# 清理 Pydantic 通用前缀和末尾标点，避免在中文错误中重复“Value error”。
def _normalize_validation_message(original_message: str) -> str:
    normalized_message = original_message.removeprefix("Value error, ").strip()
    return normalized_message.rstrip("。.")


# 将 Pydantic 枚举上下文整理为明确值列表，不把英文 or 语句直接丢给模型。
def _format_expected_values(expected_values: str) -> str:
    quoted_values = re.findall(r"'([^']*)'", expected_values)
    if quoted_values:
        return "、".join(f"`{value}`" for value in quoted_values)
    return expected_values


# 把 Pydantic 类型错误代码转换为模型可直接执行的 JSON 目标类型。
def _resolve_expected_type(
    original_type: str,
    original_message: str,
    context: dict[str, Any],
) -> str:
    expected_types = {
        "string_type": "JSON 字符串",
        "string_sub_type": "JSON 字符串",
        "int_type": "JSON 整数",
        "int_parsing": "JSON 整数",
        "int_from_float": "JSON 整数",
        "float_type": "JSON 数字",
        "float_parsing": "JSON 数字",
        "decimal_type": "JSON 数字",
        "decimal_parsing": "JSON 数字",
        "bool_type": "JSON 布尔值 true 或 false",
        "bool_parsing": "JSON 布尔值 true 或 false",
        "list_type": "JSON 数组",
        "tuple_type": "JSON 数组",
        "set_type": "JSON 数组",
        "frozenset_type": "JSON 数组",
        "dict_type": "JSON 对象",
        "mapping_type": "JSON 对象",
        "model_attributes_type": "JSON 对象",
        "json_type": "JSON 字符串",
        "none_required": "JSON null",
        "date_type": "YYYY-MM-DD 格式的 JSON 字符串",
        "date_parsing": "YYYY-MM-DD 格式的 JSON 字符串",
        "date_from_datetime_parsing": "YYYY-MM-DD 格式的 JSON 字符串",
        "datetime_type": "ISO 8601 格式的 JSON 日期时间字符串",
        "datetime_parsing": "ISO 8601 格式的 JSON 日期时间字符串",
        "time_type": "HH:MM:SS 格式的 JSON 时间字符串",
        "time_parsing": "HH:MM:SS 格式的 JSON 时间字符串",
        "uuid_type": "UUID 格式的 JSON 字符串",
        "uuid_parsing": "UUID 格式的 JSON 字符串",
    }
    expected_type = expected_types.get(original_type)
    if expected_type is not None:
        return expected_type
    class_name = context.get("class_name")
    if isinstance(class_name, str) and class_name:
        return f"符合 `{class_name}` 字段结构的 JSON 对象"
    return f"满足校验要求的值（{_normalize_validation_message(original_message)}）"


# 根据 Pydantic 返回的精确上下界或数量限制生成唯一修复动作。
def _build_constraint_feedback(
    original_type: str,
    path: str,
    original_message: str,
    context: dict[str, Any],
) -> tuple[str, str]:
    if original_type == "greater_than" and "gt" in context:
        requirement = f"大于 {context.get('gt')}"
    elif original_type == "greater_than_equal" and "ge" in context:
        requirement = f"大于或等于 {context.get('ge')}"
    elif original_type == "less_than" and "lt" in context:
        requirement = f"小于 {context.get('lt')}"
    elif original_type == "less_than_equal" and "le" in context:
        requirement = f"小于或等于 {context.get('le')}"
    elif original_type == "multiple_of" and "multiple_of" in context:
        requirement = f"为 {context.get('multiple_of')} 的倍数"
    elif original_type == "string_too_short" and "min_length" in context:
        requirement = f"长度至少为 {context.get('min_length')} 个字符"
    elif original_type == "string_too_long" and "max_length" in context:
        requirement = f"长度不超过 {context.get('max_length')} 个字符"
    elif original_type == "too_short" and "min_length" in context:
        requirement = f"至少包含 {context.get('min_length')} 项"
    elif original_type == "too_long" and "max_length" in context:
        requirement = f"最多包含 {context.get('max_length')} 项"
    elif original_type == "finite_number":
        return (
            f"字段 `{path}` 必须是有限数值，不能是 NaN 或无穷大。",
            f"将字段 `{path}` 改为有限数值。",
        )
    else:
        requirement = _normalize_validation_message(original_message)
    return (
        f"字段 `{path}` 必须{requirement}。",
        f"将字段 `{path}` 调整为{requirement}的值。",
    )


# 为每种错误类别生成包含精确字段、目标值和单一动作的中文反馈。
def _build_error_feedback(
    error_type: str,
    path: str,
    original_type: str,
    original_message: str,
    context: dict[str, Any],
) -> tuple[str, str]:
    if error_type == "invalid_json":
        parser_error = str(context.get("error") or original_message)
        return (
            f"工具参数不是合法 JSON：{parser_error}。",
            f"根据解析器定位修正 JSON 语法：{parser_error}。",
        )
    if error_type == "missing_required_field":
        return (
            f"缺少必填字段 `{path}`。",
            f"补充必填字段 `{path}`。",
        )
    if error_type == "extra_field":
        return (
            f"字段 `{path}` 未在工具 Schema 中定义。",
            f"删除字段 `{path}`。",
        )
    if error_type == "invalid_enum_value":
        expected_values = _format_expected_values(
            str(context.get("expected") or original_message)
        )
        return (
            f"字段 `{path}` 的值不合法；允许值为 {expected_values}。",
            f"将字段 `{path}` 改为以下允许值之一：{expected_values}。",
        )
    if error_type == "type_mismatch":
        expected_type = _resolve_expected_type(
            original_type,
            original_message,
            context,
        )
        return (
            f"字段 `{path}` 的数据类型错误；必须使用{expected_type}。",
            f"将字段 `{path}` 改为{expected_type}。",
        )
    if error_type == "constraint_violation":
        return _build_constraint_feedback(
            original_type,
            path,
            original_message,
            context,
        )
    if error_type == "business_rule_violation":
        normalized_message = _normalize_validation_message(original_message)
        return (
            f"字段 `{path}` 违反业务结构规则：{normalized_message[:240]}。",
            f"按以下业务结构规则修正字段 `{path}`：{normalized_message[:240]}。",
        )
    normalized_message = _normalize_validation_message(original_message)
    return (
        f"字段 `{path}` 未通过参数校验：{normalized_message[:240]}。",
        f"按以下校验要求修正字段 `{path}`：{normalized_message[:240]}。",
    )


# 把一次校验异常拆成逐项反馈，未分类内部异常停止重试而不误导模型猜测。
def build_tool_argument_error_result(
    tool_name: str,
    error: Exception,
) -> dict[str, Any]:
    details: list[dict[str, str]] = []
    is_validation_error = isinstance(error, ValidationError)
    if is_validation_error:
        for validation_error in error.errors(include_url=False, include_input=False):
            original_type = str(validation_error.get("type", "invalid_argument"))
            error_type = _classify_validation_error(original_type)
            path = _format_error_path(tuple(validation_error.get("loc", ())))
            validation_context = validation_error.get("ctx")
            context = (
                validation_context
                if isinstance(validation_context, dict)
                else {}
            )
            message, action = _build_error_feedback(
                error_type,
                path,
                original_type,
                str(validation_error.get("msg", "参数未通过校验")),
                context,
            )
            details.append(
                {
                    "error_type": error_type,
                    "field_path": path,
                    "message": message,
                    "repair_action": action,
                }
            )
    else:
        details.append(
            {
                "error_type": "validation_internal_error",
                "field_path": "$",
                "message": f"工具参数校验器发生未分类内部错误：{type(error).__name__}。",
                "repair_action": "停止参数重试，由系统处理校验器内部错误。",
            }
        )
    return {
        "status": "failure",
        "error": {
            "code": TOOL_ARGUMENT_VALIDATION_ERROR_CODE,
            "tool_name": tool_name,
            "message": "工具参数未通过 Schema 校验，本次工具调用未执行。",
            "details": details,
        },
        "retryable": is_validation_error,
        "next_action": (
            "逐项执行 details 中的 repair_action，然后重新调用同一工具并提交全部必填参数。"
            if is_validation_error
            else "停止模型重试，由系统或调用方处理校验器内部错误。"
        ),
    }


# 使用原工具调用 ID 构造标准 tool 消息，使失败结果像正常函数返回值一样进入上下文。
def build_tool_argument_error_message(
    tool_call_id: str,
    tool_name: str,
    error: Exception,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            build_tool_argument_error_result(tool_name, error),
            ensure_ascii=False,
        ),
    }


# 将已通过 Schema 但违反业务域规则的参数作为可重试工具结果返回模型。
def build_tool_policy_error_message(
    tool_call_id: str,
    tool_name: str,
    issues: Sequence[object],
) -> dict[str, str]:
    details = [
        {
            "error_type": "business_rule_violation",
            "field_path": str(getattr(issue, "field_path")),
            "message": str(getattr(issue, "message")),
            "repair_action": str(getattr(issue, "repair_action")),
        }
        for issue in issues
    ]
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": "tool_business_rule_validation_failed",
                    "tool_name": tool_name,
                    "message": "工具参数违反当前业务域规则，本次工具调用未执行。",
                    "details": details,
                },
                "retryable": True,
                "next_action": (
                    "逐项执行 details 中的 repair_action，"
                    "然后重新调用同一工具。"
                ),
            },
            ensure_ascii=False,
        ),
    }
