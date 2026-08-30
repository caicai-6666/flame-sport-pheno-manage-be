"""把 Planning 可观察的少量真实结果安全渲染为 Markdown 表格。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape as escape_html
import json
from typing import Any, Final


MARKDOWN_PREVIEW_ROW_LIMIT: Final[int] = 5
MARKDOWN_PREVIEW_CELL_MAX_LENGTH: Final[int] = 120
MARKDOWN_NULL_VALUE: Final[str] = "NULL"
_TRUNCATION_MARKER: Final[str] = "…"


# 将数据库标量或 JSON 容器稳定转换为文本，空值统一显示为明确的 NULL。
def _stringify_markdown_cell(value: Any) -> str:
    if value is None:
        return MARKDOWN_NULL_VALUE
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    return str(value)


# 在转义前截断超长文本并保留统一省略标记，避免预览挤占 Planning 上下文。
def _truncate_markdown_cell(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


# 转义会破坏表格或注入原始 HTML 的内容，并将单元格换行转换为表内安全的 <br>。
def _escape_markdown_cell(value: Any, max_length: int) -> str:
    text = _stringify_markdown_cell(value)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    truncated = _truncate_markdown_cell(normalized, max_length)
    escaped_lines = [
        escape_html(line, quote=False).replace("\\", "\\\\").replace("|", "\\|")
        for line in truncated.split("\n")
    ]
    return "<br>".join(escaped_lines)


# 按显式列键读取映射行，缺失字段显示为 NULL，避免预览因稀疏结果而列错位。
def _read_preview_row(
    row: Mapping[str, Any] | Sequence[Any],
    column_keys: Sequence[str],
) -> list[Any]:
    if isinstance(row, Mapping):
        return [row.get(column_key) for column_key in column_keys]
    if isinstance(row, (str, bytes, bytearray)):
        raise TypeError("Markdown 表格行必须是字段映射或与表头等长的值序列")
    values = list(row)
    if len(values) != len(column_keys):
        raise ValueError("Markdown 表格行的值数量必须与表头数量一致")
    return values


# 使用真实表头和最多前五行数据生成稳定 Markdown；零行时仍保留表头与分隔行。
def render_markdown_table_preview(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any] | Sequence[Any]],
    *,
    column_keys: Sequence[str] | None = None,
    max_cell_length: int = MARKDOWN_PREVIEW_CELL_MAX_LENGTH,
) -> str:
    normalized_headers = [str(header) for header in headers]
    if not normalized_headers:
        raise ValueError("Markdown 表格预览必须包含至少一个真实表头")
    if max_cell_length < len(_TRUNCATION_MARKER) + 1:
        raise ValueError("Markdown 单元格长度上限必须至少为 2")

    normalized_column_keys = (
        [str(column_key) for column_key in column_keys]
        if column_keys is not None
        else normalized_headers
    )
    if len(normalized_column_keys) != len(normalized_headers):
        raise ValueError("column_keys 数量必须与表头数量一致")

    header_line = "| " + " | ".join(
        _escape_markdown_cell(header, max_cell_length)
        for header in normalized_headers
    ) + " |"
    separator_line = "| " + " | ".join(
        "---" for _ in normalized_headers
    ) + " |"
    data_lines = []
    for row in rows[:MARKDOWN_PREVIEW_ROW_LIMIT]:
        values = _read_preview_row(row, normalized_column_keys)
        data_lines.append(
            "| "
            + " | ".join(
                _escape_markdown_cell(value, max_cell_length)
                for value in values
            )
            + " |"
        )
    return "\n".join([header_line, separator_line, *data_lines])


__all__ = [
    "MARKDOWN_NULL_VALUE",
    "MARKDOWN_PREVIEW_CELL_MAX_LENGTH",
    "MARKDOWN_PREVIEW_ROW_LIMIT",
    "render_markdown_table_preview",
]
