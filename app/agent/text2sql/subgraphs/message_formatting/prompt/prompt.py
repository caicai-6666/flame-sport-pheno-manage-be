"""构造只允许调整空白字符的用户消息格式化提示词。"""

import json

from app.agent.text2sql.shared.tool_tag_template import (
    build_tool_tag_prefixed_task_content,
)


USER_MESSAGE_FORMATTING_SYSTEM_PROMPT = """# 角色

你是查询智能体的展示格式整理器。你的唯一职责是改善一段中文消息的阅读层次。

# 允许的修改

- 可以插入、删除或调整换行、空格和缩进。
- 可以把较长内容拆成多个自然段。
- 可以通过缩进表现原文中已经存在的并列关系。

# 禁止的修改

- 禁止增加、删除、替换或调换任何非空白字符。
- 禁止增加项目符号、序号、标题、标点、Markdown 标记或解释。
- 禁止改写原文措辞、业务事实、数字、选项或提示语。
- 禁止直接输出普通文本，必须调用 `submit_formatted_user_message`。

提交前自行确认：去除全部空白字符后，格式化结果必须与原文完全一致。
"""


# 使用 JSON 字符串承载原文，避免消息中已有换行或引号破坏任务边界。
def build_user_message_formatting_messages(
    raw_text: str,
    tool_tag_template: str | None = None,
) -> list[dict[str, str]]:
    task_content = (
        "请仅通过空白字符整理下面的原文，并调用指定工具提交结果。\n\n"
        f"原文 JSON 字符串：{json.dumps(raw_text, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": USER_MESSAGE_FORMATTING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_tool_tag_prefixed_task_content(
                task_content,
                tool_tag_template,
                instruction=(
                    "下面只说明通用工具调用标签语法；具体工具名称和参数以本轮"
                    " Function Calling Schema 为准。"
                ),
                dynamic_heading="待格式化原文",
            ),
        },
    ]


__all__ = [
    "USER_MESSAGE_FORMATTING_SYSTEM_PROMPT",
    "build_user_message_formatting_messages",
]
