"""使用 Pydantic 定义结果审计子图的结论提交工具协议。"""

from typing import Final

from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)


SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME: Final[str] = "submit_query_result_audit"


# 使用审计参数模型生成标准 Function Calling 工具定义，具体调用次数由本地状态机校验。
def build_query_result_audit_tool_definition() -> dict[str, object]:
    from app.agent.text2sql.subgraphs.audit.node import QueryResultAuditAssessment

    return build_pydantic_tool_definition(
        tool_name=SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME,
        description="提交查询结果是否满足用户问题的审计结论，只能依据程序统计和受限结果样本。",
        arguments_model=QueryResultAuditAssessment,
    )


# 解析审计工具参数，并兼容部分 vLLM 服务把嵌套数组再次编码为 JSON 字符串的情况。
def parse_query_result_audit_tool_arguments(
    arguments_json: str,
) -> "QueryResultAuditAssessment":
    from app.agent.text2sql.subgraphs.audit.node import QueryResultAuditAssessment

    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        QueryResultAuditAssessment,
    )


__all__ = [
    "SUBMIT_QUERY_RESULT_AUDIT_TOOL_NAME",
    "build_query_result_audit_tool_definition",
    "parse_query_result_audit_tool_arguments",
]
