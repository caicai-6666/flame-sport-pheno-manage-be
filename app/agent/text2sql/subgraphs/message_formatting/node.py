"""实现仅调整空白字符的用户可见消息格式化子图。"""

import json
import re
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.text2sql.function_calling.feedback import (
    build_tool_argument_error_message,
)
from app.agent.text2sql.model_messages import (
    ModelMessageTraceQueue,
    create_traced_chat_completion,
)
from app.agent.text2sql.shared.model_options import (
    get_model_request_profile,
    resolve_model_provider_connection,
)
from app.agent.text2sql.shared.tool_tag_template import (
    build_tool_tag_prefixed_task_content,
    load_tool_tag_template,
    resolve_query_tool_tag_template_filename,
)
from app.agent.text2sql.subgraphs.message_formatting.prompt import (
    build_user_message_formatting_messages,
)
from app.agent.text2sql.subgraphs.message_formatting.tool import (
    SUBMIT_FORMATTED_USER_MESSAGE_TOOL_NAME,
    build_format_user_message_tool_definition,
    parse_format_user_message_arguments,
)
from app.core.config import Settings, get_settings


DEFAULT_MESSAGE_FORMATTING_MAX_TOKENS: Final[int] = 500
MAX_MESSAGE_FORMATTING_ATTEMPTS: Final[int] = 2


class UserMessageFormattingResult(BaseModel):
    """记录格式化后的兼容字符串以及是否因模型失败回退原文。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "fallback"]
    formatted_text: str = Field(description="可直接写回原有 question 或 message 字段的文本")
    attempt_count: int = Field(ge=0, description="本次格式化实际发起的模型请求次数")


class _UserMessageFormattingState(TypedDict, total=False):
    """格式化子图在入口与模型节点之间传递的最小状态。"""

    raw_text: str
    result: UserMessageFormattingResult


# 移除全部 Unicode 空白字符，用于证明模型没有改动任何业务文字或标点。
def _remove_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


# 校验格式化结果只发生空白变化，任何文字、数字、标点或顺序变化都拒绝采用。
def validate_whitespace_only_formatting(
    raw_text: str,
    formatted_text: str,
) -> None:
    if _remove_whitespace(raw_text) != _remove_whitespace(formatted_text):
        raise ValueError(
            "格式化结果修改了非空白字符；请保留原文全部字符及顺序，只调整换行、空格和缩进。"
        )


# 构造可加入同一模型上下文的工具失败结果，引导模型只修正违规的格式化内容。
def _build_content_validation_feedback(
    tool_call_id: str,
    message: str,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": "non_whitespace_content_changed",
                    "message": message,
                    "repair_action": (
                        "重新复制完整原文，只插入或调整空白字符；"
                        "不得改动任何文字、数字、标点或字符顺序。"
                    ),
                },
                "retryable": True,
            },
            ensure_ascii=False,
        ),
    }


class UserMessageFormattingSubgraph:
    """将用户可见文本整理为易读层次，失败时无条件回退原文。"""

    # 保存模型连接和追踪队列；该子图不持有业务域知识，也不改变外部消息协议。
    def __init__(
        self,
        client: Any,
        model: str,
        request_profile: str,
        message_trace_queue: ModelMessageTraceQueue | None = None,
        max_tokens: int = DEFAULT_MESSAGE_FORMATTING_MAX_TOKENS,
        close_client_after_run: bool = False,
        tool_tag_template: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._request_profile = get_model_request_profile(request_profile)
        self._message_trace_queue = message_trace_queue
        self._max_tokens = max_tokens
        self._close_client_after_run = close_client_after_run
        self._tool_tag_template = tool_tag_template

        workflow = StateGraph(_UserMessageFormattingState)
        workflow.add_node("format_user_message", self._format_user_message)
        workflow.add_edge(START, "format_user_message")
        workflow.add_edge("format_user_message", END)
        self._workflow = workflow.compile()

    # 按全局查询模型供应商创建一次性异步客户端，避免引入阶段级模型配置分叉。
    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        message_trace_queue: ModelMessageTraceQueue | None = None,
    ) -> "UserMessageFormattingSubgraph":
        resolved_settings = settings or get_settings()
        connection = resolve_model_provider_connection(resolved_settings)
        return cls(
            client=AsyncOpenAI(
                api_key=connection.api_key,
                base_url=connection.base_url,
                timeout=connection.timeout_seconds,
            ),
            model=connection.model,
            request_profile=connection.provider,
            message_trace_queue=message_trace_queue,
            close_client_after_run=True,
            tool_tag_template=load_tool_tag_template(
                resolve_query_tool_tag_template_filename(resolved_settings)
            ),
        )

    # 在至多两次模型请求内取得合法工具结果；任一协议或内容错误最终都安全回退原文。
    async def _format_user_message(
        self,
        state: _UserMessageFormattingState,
    ) -> dict[str, UserMessageFormattingResult]:
        raw_text = state["raw_text"]
        messages: list[Any] = build_user_message_formatting_messages(
            raw_text,
            self._tool_tag_template,
        )
        tool_definition = build_format_user_message_tool_definition()

        for attempt_count in range(1, MAX_MESSAGE_FORMATTING_ATTEMPTS + 1):
            response = await create_traced_chat_completion(
                client=self._client,
                message_queue=self._message_trace_queue,
                node="message_formatting",
                model=self._model,
                messages=messages,
                tools=[tool_definition],
                tool_choice="auto",
                **self._request_profile.build_non_thinking_options(self._max_tokens),
            )
            assistant_message = response.choices[0].message
            tool_calls = getattr(assistant_message, "tool_calls", None) or []
            messages.append(assistant_message)
            if len(tool_calls) != 1:
                messages.append(
                    {
                        "role": "user",
                        "content": build_tool_tag_prefixed_task_content(
                            (
                                "你没有形成唯一有效的工具调用。请只调用一次本轮"
                                " Schema 中的格式化提交工具，不要直接返回普通文本。"
                            ),
                            self._tool_tag_template,
                            instruction=(
                                "请按下面的通用标签语法形成工具调用；具体工具名称和参数"
                                "仍以本轮 Function Calling Schema 为准。"
                            ),
                            dynamic_heading="协议修复要求",
                        ),
                    }
                )
                continue
            tool_call = tool_calls[0]
            if tool_call.function.name != SUBMIT_FORMATTED_USER_MESSAGE_TOOL_NAME:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            {
                                "status": "failure",
                                "error": {
                                    "code": "unexpected_tool",
                                    "message": "调用了未注册的格式化工具。",
                                    "repair_action": "只调用 submit_formatted_user_message。",
                                },
                                "retryable": True,
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                continue
            try:
                arguments = parse_format_user_message_arguments(
                    tool_call.function.arguments
                )
                validate_whitespace_only_formatting(
                    raw_text,
                    arguments.formatted_text,
                )
            except ValidationError as error:
                messages.append(
                    build_tool_argument_error_message(
                        tool_call.id,
                        tool_call.function.name,
                        error,
                    )
                )
                continue
            except ValueError as error:
                messages.append(
                    _build_content_validation_feedback(tool_call.id, str(error))
                )
                continue
            return {
                "result": UserMessageFormattingResult(
                    status="success",
                    formatted_text=arguments.formatted_text,
                    attempt_count=attempt_count,
                )
            }

        return {
            "result": UserMessageFormattingResult(
                status="fallback",
                formatted_text=raw_text,
                attempt_count=MAX_MESSAGE_FORMATTING_ATTEMPTS,
            )
        }

    # 执行一次格式化并确保一次性客户端关闭；空字符串直接原样返回且不消耗模型请求。
    async def run(self, raw_text: str) -> UserMessageFormattingResult:
        if not raw_text:
            return UserMessageFormattingResult(
                status="fallback",
                formatted_text=raw_text,
                attempt_count=0,
            )
        try:
            state = await self._workflow.ainvoke({"raw_text": raw_text})
            return state["result"]
        except Exception:
            return UserMessageFormattingResult(
                status="fallback",
                formatted_text=raw_text,
                attempt_count=0,
            )
        finally:
            if self._close_client_after_run:
                await self._client.close()


class SettingsBackedUserMessageFormatter:
    """按查询全局设置为每条用户消息创建隔离的格式化子图。"""

    # 保存只读配置与查询级轨迹队列，不复用已经关闭的模型客户端。
    def __init__(
        self,
        settings: Settings,
        message_trace_queue: ModelMessageTraceQueue | None = None,
    ) -> None:
        self._settings = settings
        self._message_trace_queue = message_trace_queue

    # 格式化一条用户可见消息并只返回兼容字符串，模型失败由子图回退原文。
    async def format(self, raw_text: str) -> str:
        subgraph = UserMessageFormattingSubgraph.from_settings(
            settings=self._settings,
            message_trace_queue=self._message_trace_queue,
        )
        result = await subgraph.run(raw_text)
        return result.formatted_text


__all__ = [
    "SettingsBackedUserMessageFormatter",
    "UserMessageFormattingResult",
    "UserMessageFormattingSubgraph",
    "validate_whitespace_only_formatting",
]
