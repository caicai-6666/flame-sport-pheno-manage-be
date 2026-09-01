"""定义塑形子图拥有、规划层共同遵循的动态列数量文本契约。"""

import re
from typing import Literal, NamedTuple


DynamicColumnMode = Literal["not_applicable", "auto", "fixed"]
_DYNAMIC_COLUMN_COUNT_PREFIX = re.compile(
    r"^\s*[-*]\s*动态列数量\s*[：:]\s*(.*?)\s*$"
)
_FIXED_COLUMN_COUNT = re.compile(r"^固定\s*([1-9]\d*)\s*列$")
MAX_DYNAMIC_COLUMN_COUNT = 100


class DynamicColumnContract(NamedTuple):
    """表示塑形指导声明的动态列模式和固定列数。"""

    mode: DynamicColumnMode
    fixed_count: int | None


class DynamicColumnContractError(ValueError):
    """携带动态列文本契约的稳定错误代码和唯一修复说明。"""

    # 保存 Planning 可以直接执行的修复动作，避免上层从中文异常文本猜测修改方式。
    def __init__(self, code: str, message: str, repair_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.repair_action = repair_action


# 从 Markdown bullet 中读取唯一动态列数量声明，避免零行结果依赖样本推断表头。
def parse_dynamic_column_contract(guidance: str) -> DynamicColumnContract:
    declarations = [
        matched.group(1).strip()
        for line in guidance.splitlines()
        if (matched := _DYNAMIC_COLUMN_COUNT_PREFIX.fullmatch(line)) is not None
    ]
    if not declarations:
        raise DynamicColumnContractError(
            "shaping_dynamic_column_declaration_missing",
            "原料塑形指导缺少“动态列数量”声明。",
            (
                "在 shaping_guidance 中新增且只新增一条 Markdown bullet："
                "普通逐行表格写“- 动态列数量：不适用”；动态列数量随完整结果变化写"
                "“- 动态列数量：由完整结果决定”；用户明确要求 N 个动态列写"
                "“- 动态列数量：固定 N 列”。"
            ),
        )
    if len(declarations) > 1:
        raise DynamicColumnContractError(
            "shaping_dynamic_column_declaration_duplicate",
            "原料塑形指导包含多条“动态列数量”声明。",
            (
                "删除重复声明，并且只保留一条符合当前布局的声明；允许格式仅为"
                "“- 动态列数量：不适用”、“- 动态列数量：由完整结果决定”或"
                "“- 动态列数量：固定 N 列”。"
            ),
        )
    declaration = declarations[0]
    if declaration == "不适用":
        return DynamicColumnContract("not_applicable", None)
    if declaration == "由完整结果决定":
        return DynamicColumnContract("auto", None)
    fixed_match = _FIXED_COLUMN_COUNT.fullmatch(declaration)
    if fixed_match is not None:
        fixed_count = int(fixed_match.group(1))
        if fixed_count > MAX_DYNAMIC_COLUMN_COUNT:
            raise DynamicColumnContractError(
                "shaping_dynamic_column_count_out_of_range",
                (
                    "原料塑形指导要求的固定动态列数量超过系统上限："
                    f"{fixed_count} > {MAX_DYNAMIC_COLUMN_COUNT}。"
                ),
                (
                    "将固定动态列数量修改为 1 至 "
                    f"{MAX_DYNAMIC_COLUMN_COUNT} 之间的整数；如果列数应随完整结果变化，"
                    "改为“- 动态列数量：由完整结果决定”。"
                ),
            )
        return DynamicColumnContract("fixed", fixed_count)
    raise DynamicColumnContractError(
        "shaping_dynamic_column_declaration_invalid",
        f"无法识别动态列数量声明：{declaration!r}。",
        (
            "将该声明完整替换为以下一种准确格式：“- 动态列数量：不适用”、"
            "“- 动态列数量：由完整结果决定”或“- 动态列数量：固定 N 列”；"
            f"固定列数 N 必须是 1 至 {MAX_DYNAMIC_COLUMN_COUNT} 的整数。"
        ),
    )


__all__ = [
    "DynamicColumnContract",
    "DynamicColumnContractError",
    "MAX_DYNAMIC_COLUMN_COUNT",
    "parse_dynamic_column_contract",
]
