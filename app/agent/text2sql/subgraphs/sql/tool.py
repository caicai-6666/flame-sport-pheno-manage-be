"""定义 SQL 子图的提交工具和失败反馈协议。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)

if TYPE_CHECKING:
    from app.agent.text2sql.subgraphs.sql.node import (
        SqlExecutionError,
        SqlValidationError,
    )


SUBMIT_SQL_QUERY_TOOL_NAME: Final[str] = "submit_sql_query"


# 由 SQL 草稿 Pydantic 模型生成标准 Function Calling 定义，本地继续执行完整约束。
def build_sql_query_tool_definition() -> dict[str, object]:
    from app.agent.text2sql.subgraphs.sql.node import SqlQueryDraft

    return build_pydantic_tool_definition(
        tool_name=SUBMIT_SQL_QUERY_TOOL_NAME,
        description=(
            "提交一条参数化 MySQL 只读查询草稿。外部筛选值必须使用命名占位符，"
            "包括状态值、进度阈值和 NOT EXISTS 内的成员条件常量；"
            "只有 EXISTS 投影的 SELECT 1 和 LIMIT 整数可以保留字面量。"
            "parameters 使用 name/value 固定结构列表，result_columns 必须与 SELECT 输出名称和顺序一致。"
        ),
        arguments_model=SqlQueryDraft,
    )


# 对 SQL 工具参数先做原始 Pydantic 校验，失败后兼容还原嵌套 JSON 字符串再重新校验。
def parse_sql_query_tool_arguments(arguments_json: str) -> SqlQueryDraft:
    from app.agent.text2sql.subgraphs.sql.node import SqlQueryDraft

    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        SqlQueryDraft,
    )


# 将静态校验异常转换为正常工具失败结果，供模型按唯一动作修复。
def build_sql_validation_error_result(error: SqlValidationError) -> dict[str, Any]:
    retryable = error.retry_target == "sql_generation"
    next_action = {
        "sql_generation": "修正 SQL 草稿后，重新调用 submit_sql_query。",
        "query_planning": "返回查询规划阶段修正结构化查询计划。",
        "none": "停止模型重试，由系统或调用方处理该错误。",
    }[error.retry_target]
    return {
        "status": "failure",
        "error": {
            "code": error.code,
            "tool_name": SUBMIT_SQL_QUERY_TOOL_NAME,
            "message": str(error),
            "details": error.details,
            "repair_action": error.repair_action,
        },
        "retryable": retryable,
        "retry_target": error.retry_target,
        "next_action": next_action,
    }


# 使用原 SQL 工具调用 ID 返回分类校验错误，维持合法的函数调用上下文。
def build_sql_validation_error_message(
    tool_call_id: str,
    error: SqlValidationError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            build_sql_validation_error_result(error),
            ensure_ascii=False,
        ),
    }


# 将数据库执行错误转换为同一 SQL 工具调用的脱敏失败结果。
def build_sql_execution_error_message(
    tool_call_id: str,
    error: SqlExecutionError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error.code,
                    "tool_name": SUBMIT_SQL_QUERY_TOOL_NAME,
                    "message": str(error),
                    "details": error.details,
                    "repair_action": error.repair_action,
                },
                "retryable": error.retryable,
                "retry_target": "sql_generation" if error.retryable else "none",
                "next_action": (
                    "按 repair_action 修正 SQL 后重新调用 submit_sql_query。"
                    if error.retryable
                    else "停止模型重试，由系统处理数据库基础设施错误。"
                ),
            },
            ensure_ascii=False,
        ),
    }


# 将无合法工具调用的响应转换为可重试上下文，避免偶发协议失败直接终止查询。
def build_sql_protocol_retry_message(
    error_code: str,
    message: str,
    repair_action: str,
    tool_call_id: str | None = None,
    tool_tag_template: str | None = None,
) -> dict[str, str]:
    error: dict[str, Any] = {
        "code": error_code,
        "tool_name": SUBMIT_SQL_QUERY_TOOL_NAME,
        "message": message,
        "repair_action": repair_action,
    }
    if tool_call_id is None and tool_tag_template is not None:
        error["tool_call_format_guidance"] = {
            "instruction": (
                "严格仿照 template 输出唯一 submit_sql_query 调用，"
                "并将 SQL 草稿作为合法 JSON 参数提交。"
            ),
            "template": tool_tag_template,
        }
    content = json.dumps(
        {
            "status": "failure",
            "error": error,
            "retryable": True,
            "retry_target": "sql_generation",
            "next_action": "按 repair_action 重新生成，并且只调用一次 submit_sql_query。",
        },
        ensure_ascii=False,
    )
    if tool_call_id is not None:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }
    return {"role": "user", "content": content}


__all__ = [
    "SUBMIT_SQL_QUERY_TOOL_NAME",
    "build_sql_execution_error_message",
    "build_sql_protocol_retry_message",
    "build_sql_query_tool_definition",
    "build_sql_validation_error_message",
    "build_sql_validation_error_result",
    "parse_sql_query_tool_arguments",
]
