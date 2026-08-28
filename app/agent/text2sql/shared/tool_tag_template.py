"""从受控项目目录加载模型工具调用格式提示。"""

from pathlib import Path
from typing import Final


PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
TOOL_TAG_TEMPLATE_DIRECTORY: Final[Path] = PROJECT_ROOT / "data" / "tool-tag"
MAX_TOOL_TAG_TEMPLATE_CHARACTERS: Final[int] = 16_000


# 将通用标签语法置于动态任务之前，调用方只负责提供本阶段指令和任务标题。
def build_tool_tag_prefixed_task_content(
    dynamic_content: str,
    tool_tag_template: str | None,
    instruction: str,
    dynamic_heading: str,
) -> str:
    if tool_tag_template is None:
        return dynamic_content
    return (
        "# 工具调用格式提醒\n\n"
        f"{instruction}\n\n"
        f"{tool_tag_template}\n\n"
        f"# {dynamic_heading}\n\n"
        f"{dynamic_content}"
    )


# 只允许选择 tool-tag 目录下的单个文本文件，阻止环境变量形成任意文件读取。
def load_tool_tag_template(template_filename: str | None) -> str | None:
    if template_filename is None or not template_filename.strip():
        return None
    normalized_filename = template_filename.strip()
    candidate_name = Path(normalized_filename)
    if (
        candidate_name.name != normalized_filename
        or candidate_name.suffix.lower() != ".txt"
    ):
        raise ValueError(
            "工具调用模板必须是 data/tool-tag 目录下的单个 .txt 文件名"
        )
    template_directory = TOOL_TAG_TEMPLATE_DIRECTORY.resolve()
    template_path = (template_directory / candidate_name.name).resolve()
    if template_path.parent != template_directory:
        raise ValueError("工具调用模板必须位于 data/tool-tag 目录内")
    if not template_path.is_file():
        raise FileNotFoundError(f"工具调用模板不存在：{template_path}")
    template_content = template_path.read_text(encoding="utf-8").strip()
    if not template_content:
        raise ValueError(f"工具调用模板不能为空：{template_path}")
    if len(template_content) > MAX_TOOL_TAG_TEMPLATE_CHARACTERS:
        raise ValueError(
            "工具调用模板内容过长，最多允许 "
            f"{MAX_TOOL_TAG_TEMPLATE_CHARACTERS} 个字符"
        )
    return template_content


# 读取整条查询流水线唯一的工具标签模板，禁止阶段级配置形成协议分叉。
def resolve_query_tool_tag_template_filename(settings: object) -> str | None:
    return getattr(settings, "agent_query_tool_tag_template", None)
