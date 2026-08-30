"""定义塑形子图拥有、规划层共同遵循的动态列数量文本契约。"""

import re
from typing import Literal, NamedTuple


DynamicColumnMode = Literal["not_applicable", "auto", "fixed"]
_DYNAMIC_COLUMN_COUNT_PREFIX = re.compile(
    r"^\s*[-*]\s*动态列数量\s*[：:]\s*(.*?)\s*$"
)
_FIXED_COLUMN_COUNT = re.compile(r"^固定\s*([1-9]\d{0,2})\s*列$")


class DynamicColumnContract(NamedTuple):
    """表示塑形指导声明的动态列模式和固定列数。"""

    mode: DynamicColumnMode
    fixed_count: int | None


class DynamicColumnContractError(ValueError):
    """表示塑形指导没有遵守唯一的动态列数量声明格式。"""


# 从 Markdown bullet 中读取唯一动态列数量声明，避免零行结果依赖样本推断表头。
def parse_dynamic_column_contract(guidance: str) -> DynamicColumnContract:
    declarations = [
        matched.group(1).strip()
        for line in guidance.splitlines()
        if (matched := _DYNAMIC_COLUMN_COUNT_PREFIX.fullmatch(line)) is not None
    ]
    if not declarations:
        raise DynamicColumnContractError(
            "原料塑形指导缺少“动态列数量”声明。"
        )
    if len(declarations) > 1:
        raise DynamicColumnContractError(
            "原料塑形指导只能包含一条“动态列数量”声明。"
        )
    declaration = declarations[0]
    if declaration == "不适用":
        return DynamicColumnContract("not_applicable", None)
    if declaration == "由完整结果决定":
        return DynamicColumnContract("auto", None)
    fixed_match = _FIXED_COLUMN_COUNT.fullmatch(declaration)
    if fixed_match is not None:
        return DynamicColumnContract("fixed", int(fixed_match.group(1)))
    raise DynamicColumnContractError(
        "“动态列数量”只能写为“固定 N 列”“由完整结果决定”或“不适用”。"
    )


__all__ = [
    "DynamicColumnContract",
    "DynamicColumnContractError",
    "parse_dynamic_column_contract",
]
