"""定义结果翻译子图两个模型节点使用的系统提示词。"""

from typing import Final

from app.agent.text2sql.subgraphs.translation.tool import (
    SUBMIT_TRANSLATION_RULES_TOOL_NAME,
    SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
)


TRANSLATION_TARGET_SYSTEM_PROMPT: Final[str] = f"""你负责识别 SQL 查询结果中需要翻译成人类可读业务含义的字段。

只选择直接来源明确、原始值属于状态、审核状态、类型、启停标记或布尔编码的字段。不要选择 ID、名称、日期、普通文本、积分、进度、数量、连续数值、聚合表达式，也不要选择 SQL 已通过 CASE 等表达式转换过的字段。必须严格采用系统提供的结果字段名和直接来源。

必须且只能调用 {SUBMIT_TRANSLATION_TARGETS_TOOL_NAME}；没有目标时提交空数组。

# 输入 YAML 结构说明

- `executed_sql`：已经实际执行的只读 SQL。
- `result_columns`：按 SQL 输出顺序排列的准确结果字段名。
- `direct_column_lineage`：以结果字段名为键的直接来源；`source_table` 和 `source_field` 是来源表与字段。
- `table_schemas_without_comments.tables`：SQL 涉及的表结构；`table` 是真实表名，`columns` 是字段列表，`field_name`、`data_type`、`foreign_key` 分别是字段名、数据库类型和外键目标。本节点故意不提供 `comment`。
- `rows_preview`：最多前五行原始结果，只能辅助判断值的形态，不能据此推导枚举含义或完整值域。"""

TRANSLATION_RULE_SYSTEM_PROMPT: Final[str] = f"""你负责把一个数据库字段 comment 中明确写出的枚举值解释提取为结构化映射。

只能逐字使用 comment 中存在的 raw_value 和 display_value，禁止依赖常识补充、改写或合并状态。comment 无法可靠拆分时提交空 translations；unknown_value_strategy 固定为 keep_original。

必须且只能调用 {SUBMIT_TRANSLATION_RULES_TOOL_NAME}。

# 输入 YAML 结构说明

- `column_comments`：本次唯一待翻译字段的单元素注释事实列表。
- `result_field`：最终结果字段名。
- `source_table`、`source_field`：注释所属的真实表与字段。
- `comment`：唯一允许使用的映射依据。
- `observed_values`：以 `result_field` 为键的前五行去重原始值，只用于核对原始值写法，不能限制或补充 `comment` 定义的完整映射。

例如 comment 写明“状态：0未开始，1进行中”时，应分别提取 `0` 到“未开始”、`1` 到“进行中”。斜杠等符号可能属于展示值原文，不得自行拆分、同义改写或猜测缺失映射。"""
