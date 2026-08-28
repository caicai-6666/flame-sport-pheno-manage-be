"""使用 Pydantic 定义结果翻译子图的目标识别与映射提交工具。"""

from collections.abc import Collection
from typing import Final

from app.agent.text2sql.shared.tools.argument_compatibility import (
    validate_tool_arguments_with_embedded_json_fallback,
)
from app.agent.text2sql.shared.tools.pydantic_schema import (
    build_pydantic_tool_definition,
)


SUBMIT_TRANSLATION_TARGETS_TOOL_NAME: Final[str] = "submit_translation_targets"
SUBMIT_TRANSLATION_RULES_TOOL_NAME: Final[str] = "submit_translation_rules"


# 把来源表字段收紧为当前业务域白名单，远端提示与本地语义校验共用同一范围。
def _apply_source_table_enum(
    definition: dict[str, object],
    nested_model_name: str,
    allowed_tables: Collection[str],
) -> dict[str, object]:
    function = definition["function"]
    assert isinstance(function, dict)
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    nested_models = parameters["$defs"]
    assert isinstance(nested_models, dict)
    nested_model = nested_models[nested_model_name]
    assert isinstance(nested_model, dict)
    properties = nested_model["properties"]
    assert isinstance(properties, dict)
    source_table = properties["source_table"]
    assert isinstance(source_table, dict)
    source_table["enum"] = list(allowed_tables)
    return definition


# 构造节点 1 的标准 Pydantic 工具，字段说明直接进入 OpenAI Function Calling Schema。
def build_translation_targets_tool_definition(
    allowed_tables: Collection[str],
) -> dict[str, object]:
    from app.agent.text2sql.subgraphs.translation.node import (
        TranslationTargetSubmission,
    )

    definition = build_pydantic_tool_definition(
        tool_name=SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
        description=(
            "提交需要把数据库编码翻译成人类可读含义的结果字段及其直接来源；"
            "没有时提交空数组。"
        ),
        arguments_model=TranslationTargetSubmission,
    )
    return _apply_source_table_enum(definition, "TranslationTarget", allowed_tables)


# 构造节点 2 的标准 Pydantic 工具，映射来源表继续受当前业务域白名单约束。
def build_translation_rules_tool_definition(
    allowed_tables: Collection[str],
) -> dict[str, object]:
    from app.agent.text2sql.subgraphs.translation.node import TranslationRuleSubmission

    definition = build_pydantic_tool_definition(
        tool_name=SUBMIT_TRANSLATION_RULES_TOOL_NAME,
        description="仅根据系统提供的字段 comment 提交原始值到展示含义的翻译规则。",
        arguments_model=TranslationRuleSubmission,
    )
    return _apply_source_table_enum(
        definition,
        "ColumnTranslationRule",
        allowed_tables,
    )


# 解析节点 1 工具参数，并兼容部分 vLLM 模型把嵌套对象再次编码为 JSON 字符串的情况。
def parse_translation_targets_tool_arguments(
    arguments_json: str,
) -> "TranslationTargetSubmission":
    from app.agent.text2sql.subgraphs.translation.node import (
        TranslationTargetSubmission,
    )

    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        TranslationTargetSubmission,
    )


# 解析节点 2 工具参数，并在 Pydantic 校验前递归还原嵌套 JSON 字符串。
def parse_translation_rules_tool_arguments(
    arguments_json: str,
) -> "TranslationRuleSubmission":
    from app.agent.text2sql.subgraphs.translation.node import TranslationRuleSubmission

    return validate_tool_arguments_with_embedded_json_fallback(
        arguments_json,
        TranslationRuleSubmission,
    )


__all__ = [
    "SUBMIT_TRANSLATION_RULES_TOOL_NAME",
    "SUBMIT_TRANSLATION_TARGETS_TOOL_NAME",
    "build_translation_rules_tool_definition",
    "build_translation_targets_tool_definition",
    "parse_translation_rules_tool_arguments",
    "parse_translation_targets_tool_arguments",
]
