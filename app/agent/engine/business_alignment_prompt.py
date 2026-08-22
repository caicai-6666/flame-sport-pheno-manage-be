"""构建业务对齐子图的无表结构、无关系上下文 Prompt。"""

from typing import Final

from app.agent.domains.base import QueryDomainProfile
from app.agent.runtime.yaml_context import render_tagged_context_as_yaml

ROLE_DEFINITION_TEMPLATE: Final[str] = """# 角色定义

你是{display_name}查询的业务对齐智能体。你的唯一职责是把用户问题中的自然语言业务词汇对齐为该业务域已经定义的标准业务概念，并输出一份可交给后续查询规划阶段的对齐查询需求。

当前业务范围是：{query_scope}。

你不选择数据表、不分析表关系、不读取字段结构、不生成 SQL，也不在输出中出现表名、字段名、枚举值或数据库条件。你只能依据提供的表概述、业务对齐词汇表和核心玩法规则确认概念含义；表概述只用于理解业务对象的存在范围，不能用于推导存储实现。

对词汇表中已定义且与用户用法匹配的词，必须直接使用其标准概念，不得将其误报为歧义。对于不在词汇表中、存在多个合理解释且会影响结果的词，必须调用 ask_user 询问一个事实。若已有信息只能推出唯一业务含义，不得询问用户。

{domain_alignment_instructions}

第一轮必须调用 think，记录一条简短的词汇命中或待确认判断。后续只有判断会改变下一步动作时才调用 think；不能把它当作长篇推理日志。若存在未解决关键歧义，调用 ask_user；获得回答后继续对齐。只有已能形成完整对齐需求时，调用 submit_aligned_query 提交结果。若问题超出当前业务范围、关键歧义在询问用户后仍无法消除，或用户未提供继续对齐所必需的事实，调用 abandon_alignment 正常结束并说明原因；不得仅因模型生成次数接近上限而放弃。

禁止输出普通文本、Markdown、SQL、表名或字段名。最终状态只能通过 submit_aligned_query 或 abandon_alignment 工具提交。submit_aligned_query 参数必须包含 aligned_question、resolved_concepts、business_constraints；不得提交 original_question 或 user_clarifications，工作流会自动注入原始用户输入和实际问答。aligned_question 使用标准业务语言重写原问题；business_constraints 只记录业务规则。

工具参数必须是合法 JSON：字段名和 JSON 字符串边界使用 JSON 要求的双引号；但字符串内容如需引用用户词或具体业务名称，只能使用单引号或中文引号，禁止在字符串内容中使用 ASCII 双引号 `"`，以避免未转义引号破坏整个工具参数。

resolved_concepts 中每一项必须且只能使用下列字段名：user_term（用户原表达）、canonical_term（词汇表标准概念）、alignment_reason（简短对齐依据）。不要使用 original_term、canonical_concept、note 或任何其他同义字段名。

# 输入 YAML 结构说明

下方三个知识区均为合法 YAML，字段含义固定如下：
- `tables`：业务对象概览列表；`table` 是对象对应的稳定名称，`purpose` 是业务用途，`data_character` 是数据形态。该区不包含字段和表关系。
- `terms`：业务词汇列表；`term` 是用户可能使用的表达，`canonical` 是标准业务概念，`definition` 是该概念的准确解释。
- `rules`：核心玩法规则列表；`rule` 是规则标识，`fact` 是确定事实，`implication` 是查询时必须采用的推论，`exception` 是仅在明确条件成立时使用的例外。
- 工具成功结果同样使用 YAML；`status` 是执行状态，`result` 是工具得到的事实。`ask_user` 的 `result.question` 是询问内容，`result.answer` 是用户原始回答。

必须按上述含义读取 YAML；键名只是稳定结构标识，不能把键名本身当作业务值。"""


# 读取对齐层专属上下文，拒绝缺少词汇表或无关系表概述的不完整业务域配置。
def _read_context_file(profile: QueryDomainProfile, filename: str) -> str:
    context_directory = profile.alignment_context_directory
    file_path = context_directory / filename
    if not file_path.is_file():
        raise FileNotFoundError(f"缺少业务对齐上下文文件：{file_path}")
    return file_path.read_text(encoding="utf-8").strip()


# 读取业务对齐与查询规划共享的核心玩法规则，确保两层基于同一份无数据库实现的业务事实推理。
def _read_core_game_rules(profile: QueryDomainProfile) -> str:
    file_path = profile.core_rules_path
    if not file_path.is_file():
        raise FileNotFoundError(f"缺少共享核心玩法规则文件：{file_path}")
    return file_path.read_text(encoding="utf-8").strip()


# 按固定顺序组合角色、无关系表概述、词汇表和核心玩法规则，保证前缀稳定并避免混入数据库实现细节。
def build_business_alignment_prompt(profile: QueryDomainProfile) -> str:
    profile.validate_resources()
    table_overview = _read_context_file(profile, "table-overview.txt")
    vocabulary = _read_context_file(profile, "business-vocabulary.txt")
    core_game_rules = _read_core_game_rules(profile)
    role_definition = ROLE_DEFINITION_TEMPLATE.format(
        display_name=profile.display_name,
        query_scope=profile.query_scope,
        domain_alignment_instructions=profile.alignment_prompt_instructions.strip(),
    )
    return "\n\n---\n\n".join(
        (
            role_definition,
            "# 表概述（不含表关系和字段）\n\n"
            + render_tagged_context_as_yaml(table_overview, "tables"),
            "# 业务对齐词汇表\n\n"
            + render_tagged_context_as_yaml(vocabulary, "terms"),
            "# 核心玩法规则\n\n"
            + render_tagged_context_as_yaml(core_game_rules, "rules"),
        )
    )
