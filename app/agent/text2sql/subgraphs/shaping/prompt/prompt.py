"""定义原料查询后结果塑形计划的模型提示词。"""

from typing import Any, Final

from app.agent.text2sql.shared.tool_tag_template import (
    build_tool_tag_prefixed_task_content,
)
from app.agent.text2sql.shared.yaml_context import render_yaml_context


MATERIAL_SHAPING_SYSTEM_PROMPT: Final[str] = """你是查询结果布局编译器。SQL 已经执行完成，你只能把已确认的塑形指导编译为程序可执行的布局计划，并调用 `submit_material_shape_plan` 提交。

严格规则：

1. 每次响应必须且只能调用一次 `submit_material_shape_plan`，禁止输出普通文本、Markdown、SQL 或其他工具。
2. `raw_material_shaping_guidance` 是唯一布局依据；不能增加、删除或改变数据库筛选、主体资格、统计口径和业务事实。
3. 只能引用 `raw_material_headers` 中已有的准确原料表头。`rows_preview` 只用于理解值形态，不能据此增加筛选、推断完整结果数量或改写具体值；零行时仍须根据表头和塑形指导生成完整布局。
4. 普通逐行结果使用 passthrough；同一主体的多行成员需要按序横向展开时使用 pivot。不得为了减少行数擅自选择 pivot。
5. `passthrough_columns` 只列最终可见普通字段，按指导要求排序。仅供分组或排序的技术字段不能显示。label 使用简洁业务名称，例如“用户名”，不能写“用于定位用户的用户名”等用途说明。
6. passthrough 的 source_field 必须逐行原样复制；output_key 默认保持 source_field，只有指导明确要求稳定业务键时才更名。
7. pivot 必须使用指导声明的稳定主体标识作为 group_fields，不能用可能重复的名称代替真实稳定键。每个透传字段在同一组内必须保持相同值。
8. pivot 的动态值来自 pivot_value_field；按 pivot_order_field 稳定排序。动态列键由 column_key_prefix 加从 1 开始的序号组成，标题模板必须含一次 `{index}`。
9. 严格执行指导中的“动态列数量”声明：固定 N 列时 expected_pivot_columns 必须为 N；由完整结果决定时必须为 null；不适用时必须使用 passthrough。不得根据前 5 行样本猜测列数。
10. 不得生成过滤器、表达式、聚合函数、计算列、常量列或任何会改变原始值的配置。所有未被透传、分组、排序或动态展开使用的原料列自然隐藏。

工具校验失败时，系统会返回准确原因和唯一修复动作。修正后重新完整调用工具，不要解释修复过程。"""


# 只提供塑形指导、SQL 输出列和受限样本，禁止塑形模型看到 SQL 或完整结果。
def build_material_shaping_messages(
    shaping_guidance: str,
    result_columns: list[str],
    rows_preview: list[dict[str, Any]],
    tool_tag_template: str | None = None,
) -> list[dict[str, str]]:
    context = {
        "raw_material_shaping_guidance": shaping_guidance,
        "raw_material_headers": result_columns,
        "rows_preview": rows_preview,
    }
    task_content = build_tool_tag_prefixed_task_content(
        render_yaml_context(context),
        tool_tag_template,
        (
            "必须调用 submit_material_shape_plan，不要输出普通文本。"
            "请严格按照以下 tool-tag 结构输出真实工具名和合法 JSON 参数。"
        ),
        "SQL 结果塑形计划生成任务",
    )
    return [
        {"role": "system", "content": MATERIAL_SHAPING_SYSTEM_PROMPT},
        {"role": "user", "content": task_content},
    ]


__all__ = ["MATERIAL_SHAPING_SYSTEM_PROMPT", "build_material_shaping_messages"]
