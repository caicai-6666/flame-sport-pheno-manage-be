"""构造与业务域、子图和工具无关的系统指导消息。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent.text2sql.shared.yaml_context import render_yaml_context


SystemGuidanceText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
]


class SystemGuidancePayload(BaseModel):
    """表示系统以用户角色向模型发送的高优先级运行时指导。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_type: Literal["system_guidance"] = Field(
        default="system_guidance",
        description="固定消息类型，用于区分系统指导与最终用户的业务输入。",
    )
    guidance: SystemGuidanceText = Field(
        description="模型在现有系统约束内应优先遵循的当前动作指导。",
    )


# 把任意节点提供的系统指导统一构造为 user 角色 YAML，避免误用中途 system 角色。
def build_system_guidance_message(guidance: str) -> dict[str, str]:
    payload = SystemGuidancePayload(guidance=guidance)
    return {
        "role": "user",
        "content": render_yaml_context(payload),
    }


__all__ = ["SystemGuidancePayload", "build_system_guidance_message"]
