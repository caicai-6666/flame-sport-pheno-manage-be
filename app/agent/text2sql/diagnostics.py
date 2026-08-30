"""向 Docker 标准输出写入分级的 Text-to-SQL 诊断事件。"""

import json
import logging
import re
import time
import traceback
from typing import Any, Literal

from app.agent.text2sql.events.models import AgentProgressUpdate
from app.agent.text2sql.model_messages import ModelMessageTraceEntry


DIAGNOSTIC_LOGGER_NAME = "uvicorn.error.agent_query_diagnostic"
_logger = logging.getLogger(DIAGNOSTIC_LOGGER_NAME)
_MODEL_TEXT_PATTERN = re.compile(r"模型文本：.*", re.DOTALL)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9_-]+|bearer\s+[A-Za-z0-9._-]+)\b"
)
_SAFE_EXCEPTION_TYPES = frozenset(
    {
        "BusinessAlignmentExecutionError",
        "QueryPlanningExecutionError",
        "SqlExecutionError",
        "SqlValidationError",
    }
)


# 对完整消息载荷统一遮蔽认证信息，保留模型实际收到和返回的业务内容用于排障。
def _redact_model_message_payload(payload: Any) -> Any:
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    return json.loads(_SECRET_PATTERN.sub("[REDACTED]", serialized))


# 将允许记录的内部异常摘要做长度限制和敏感片段替换，模型普通文本不会进入日志。
def _build_safe_exception_summary(error: BaseException) -> str | None:
    if type(error).__name__ not in _SAFE_EXCEPTION_TYPES:
        return None
    summary = _MODEL_TEXT_PATTERN.sub("模型文本已省略", str(error))
    return _SECRET_PATTERN.sub("[REDACTED]", summary)[:500]


# 提取异常最终落点，保留定位代码所需的文件、函数和行号而不序列化局部变量。
def _build_exception_location(error: BaseException) -> dict[str, object]:
    extracted = traceback.extract_tb(error.__traceback__)
    if not extracted:
        return {}
    frame = extracted[-1]
    return {
        "exception_file": frame.filename.rsplit("/", 1)[-1],
        "exception_function": frame.name,
        "exception_line": frame.lineno,
    }


class AgentQueryDiagnosticLogger:
    """按查询记录 ID 输出结构化、脱敏且可通过环境变量关闭的诊断日志。"""

    # 保存查询身份、诊断等级和起始时间；不保留用户问题、模型响应或结果数据。
    def __init__(
        self,
        query_id: str,
        domain_key: str,
        enabled: bool,
        level: Literal["basic", "detailed", "trace"],
    ) -> None:
        self._query_id = query_id
        self._domain_key = domain_key
        self._enabled = enabled
        self._level = level
        self._started_at = time.monotonic()
        self._last_stage = "accepted"

    # 以单行 JSON 输出稳定诊断事件，方便 Docker 日志按 query_id 和 event 检索。
    def _write(self, event: str, **details: object) -> None:
        if not self._enabled:
            return
        payload = {
            "component": "agent_query",
            "event": event,
            "query_id": self._query_id,
            "domain_key": self._domain_key,
            **details,
        }
        _logger.info(
            "agent_query_diagnostic %s",
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
        )

    # 记录查询创建事实，只保存问题长度以证明输入存在，不保存问题正文和个人信息。
    def query_started(self, question_length: int) -> None:
        self._write(
            "query_started",
            stage="accepted",
            status="running",
            question_length=question_length,
            diagnostic_level=self._level,
        )

    # 记录前台已经发布的阶段变化，但丢弃可能含业务文本的 message 和 payload。
    def progress(self, update: AgentProgressUpdate) -> None:
        self._last_stage = update.stage
        self._write(
            "workflow_progress",
            stage=update.stage,
            workflow_event=update.event_type,
            status=update.status,
            title=update.title,
        )

    # 记录用户完成一次交互的协议事实，不保存交互问题、选项和回答正文。
    def interaction_answered(self, interaction_type: str) -> None:
        self._write(
            "interaction_answered",
            stage=self._last_stage,
            status="success",
            interaction_type=interaction_type,
        )

    # 判断当前查询是否需要建立模型消息队列，关闭时不在内存保留原始业务上下文。
    def model_message_trace_enabled(self) -> bool:
        return self._enabled and self._level == "trace"

    # 将统一消息队列中的请求和响应按原序写入诊断日志，不再接受业务层拼接轨迹。
    def model_message_trace(self, entry: ModelMessageTraceEntry) -> None:
        if not self._enabled or self._level != "trace":
            return
        self._write(
            "model_message_trace",
            stage=entry.node,
            status="running",
            message_sequence=entry.sequence,
            message_direction=entry.direction,
            message_payload=_redact_model_message_payload(entry.payload),
        )

    # 汇总各子图的安全元数据；详细模式只增加表、字段、参数化 SQL 和次数，不记录数据值。
    def pipeline_finished(self, result: Any) -> None:
        details: dict[str, object] = {
            "stage": self._last_stage,
            "status": getattr(result, "status", "unknown"),
            "elapsed_ms": round((time.monotonic() - self._started_at) * 1000),
        }
        alignment_result = getattr(result, "alignment_result", None)
        planning_result = getattr(result, "planning_result", None)
        sql_result = getattr(result, "sql_result", None)
        translation_result = getattr(result, "translation_result", None)
        shaping_result = getattr(result, "shaping_result", None)
        audit_result = getattr(result, "audit_result", None)
        if alignment_result is not None:
            details["alignment_status"] = alignment_result.status
            details["alignment_generations"] = alignment_result.generation_count
        if planning_result is not None:
            details["planning_status"] = planning_result.status
            details["planning_generations"] = planning_result.generation_count
            details["planning_tool_calls"] = planning_result.tool_call_count
        if sql_result is not None:
            details.update(
                {
                    "sql_status": sql_result.status,
                    "sql_error_code": sql_result.error_code,
                    "sql_retry_target": sql_result.retry_target,
                    "sql_generations": sql_result.generation_count,
                    "returned_row_count": sql_result.returned_row_count,
                    "planned_limit": sql_result.planned_limit,
                    "effective_limit": sql_result.effective_limit,
                }
            )
        if translation_result is not None:
            details["translation_status"] = translation_result.status
            details["translation_target_count"] = len(translation_result.targets)
            details["translation_rule_count"] = len(translation_result.rules)
        if shaping_result is not None:
            details["shaping_status"] = shaping_result.status
            details["shaping_source_row_count"] = shaping_result.source_row_count
            details["shaping_result_row_count"] = shaping_result.result_row_count
        if audit_result is not None:
            details["audit_status"] = audit_result.status
            details["audit_matches_user_request"] = (
                audit_result.assessment.matches_user_request
                if audit_result.assessment is not None
                else None
            )
        if self._level in {"detailed", "trace"}:
            self._add_detailed_result_metadata(details, planning_result, sql_result)
        self._write("pipeline_finished", **details)

    # 在详细模式补充查询结构和已校验 SQL；不读取筛选参数值、原始响应或结果行。
    def _add_detailed_result_metadata(
        self,
        details: dict[str, object],
        planning_result: Any,
        sql_result: Any,
    ) -> None:
        query_plan = (
            getattr(planning_result, "query_plan", None)
            if planning_result is not None
            else None
        )
        material_plan = (
            getattr(planning_result, "material_plan", None)
            if planning_result is not None
            else None
        )
        if query_plan is not None:
            details["planned_tables"] = [
                table.table_name for table in query_plan.tables
            ]
            details["planned_result_fields"] = [
                field.result_field for field in query_plan.select_fields
            ]
            details["query_block_count"] = len(query_plan.query_blocks)
        elif material_plan is not None:
            details["planned_tables"] = list(material_plan.required_tables)
            details["planning_protocol"] = "interactive_material_tools"
        if sql_result is not None:
            details["result_columns"] = list(sql_result.result_columns)
            if sql_result.status == "success" and sql_result.sql is not None:
                details["validated_sql_template"] = sql_result.sql[:20_000]
            if sql_result.error_diagnostic is not None:
                details["external_error"] = {
                    "exception_type": sql_result.error_diagnostic.exception_type,
                    "status_code": sql_result.error_diagnostic.status_code,
                    "provider_code": sql_result.error_diagnostic.provider_code,
                }

    # 记录未形成子图终态的异常类型和代码落点；仅白名单内部异常允许输出脱敏摘要。
    def query_failed_with_exception(self, error: BaseException) -> None:
        details: dict[str, object] = {
            "stage": self._last_stage,
            "status": "failure",
            "elapsed_ms": round((time.monotonic() - self._started_at) * 1000),
            "exception_type": type(error).__name__,
            **_build_exception_location(error),
        }
        safe_summary = _build_safe_exception_summary(error)
        if self._level in {"detailed", "trace"} and safe_summary is not None:
            details["safe_error_summary"] = safe_summary
        self._write("query_failed_with_exception", **details)

    # 记录没有结构化异常对象的取消、交互超时等终态，不保存用户输入内容。
    def query_terminated(self, status: str, reason_code: str) -> None:
        self._write(
            "query_terminated",
            stage=self._last_stage,
            status=status,
            reason_code=reason_code,
            elapsed_ms=round((time.monotonic() - self._started_at) * 1000),
        )
