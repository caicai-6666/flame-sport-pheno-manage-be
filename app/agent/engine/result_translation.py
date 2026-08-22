"""查询结果翻译子图：识别枚举字段、提取注释映射并确定性翻译完整结果。"""

import json
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlglot import exp, parse_one

from app.agent.domains.base import QueryDomainProfile
from app.agent.runtime.yaml_context import parse_yaml_context, render_yaml_context
from app.agent.runtime.model_options import (
    DEFAULT_TRANSLATION_MAX_TOKENS,
    build_non_thinking_completion_options,
    build_strict_tools_base_url,
)
from app.agent.runtime.table_schema_cache import CachingTableSchemaReader
from app.agent.runtime.table_schema_reader import InformationSchemaTableSchemaReader
from app.agent.tools.argument_feedback import build_tool_argument_error_message
from app.agent.tools.strict_schema import build_strict_tool_definition
from app.agent.tools.table_schema import AllowedTableName, TableSchemaToolResponse
from app.core.config import Settings, get_settings


RESULT_PREVIEW_ROW_COUNT: Final[int] = 5
MAX_TOOL_ARGUMENT_REPAIR_COUNT: Final[int] = 1
MAX_PARALLEL_TRANSLATION_FIELD_COUNT: Final[int] = 4
SUBMIT_TRANSLATION_TARGETS_TOOL_NAME: Final[str] = "submit_translation_targets"
SUBMIT_TRANSLATION_RULES_TOOL_NAME: Final[str] = "submit_translation_rules"
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

SchemaReader = Callable[[str], TableSchemaToolResponse]


class TranslationTarget(BaseModel):
    """节点 1 识别出的一个需要翻译且可追溯到直接来源列的结果字段。"""

    model_config = ConfigDict(extra="forbid")

    result_field: str = Field(description="最终 SQL 结果中的准确字段名或 AS 别名")
    source_table: AllowedTableName = Field(description="该结果字段直接来源的真实表名")
    source_field: str = Field(description="该结果字段直接来源的真实字段名")
    reason: str = Field(description="该字段为何属于需要翻译的枚举、状态或布尔编码")


class TranslationTargetSubmission(BaseModel):
    """节点 1 通过 strict 工具提交的全部翻译目标，允许没有待翻译字段。"""

    model_config = ConfigDict(extra="forbid")

    targets: list[TranslationTarget] = Field(description="需要翻译的结果字段；没有时为空数组")


class TranslationMappingItem(BaseModel):
    """字段注释中一个原始数据库值与中文展示值的确定映射。"""

    model_config = ConfigDict(extra="forbid")

    raw_value: str = Field(description="注释中定义的原始数据库值，保持原文")
    display_value: str = Field(description="注释中定义的对应业务含义，保持原文")


class ColumnTranslationRule(BaseModel):
    """节点 2 针对一个结果字段生成的完整注释翻译规则。"""

    model_config = ConfigDict(extra="forbid")

    result_field: str = Field(description="要应用翻译的最终 SQL 结果字段")
    source_table: AllowedTableName = Field(description="字段注释所属的真实表名")
    source_field: str = Field(description="字段注释所属的真实字段名")
    mappings: list[TranslationMappingItem] = Field(
        description="只从字段注释逐项提取的原始值与展示值映射"
    )
    unknown_value_strategy: Literal["keep_original"] = Field(
        description="注释未定义的值必须保留原值"
    )


class TranslationRuleSubmission(BaseModel):
    """节点 2 通过 strict 工具提交的全部字段翻译规则。"""

    model_config = ConfigDict(extra="forbid")

    translations: list[ColumnTranslationRule] = Field(
        description="能够从注释可靠提取的翻译规则；无法提取时省略对应字段"
    )


class ColumnCommentFact(BaseModel):
    """节点 2 从结构读取器取得的单个字段注释事实。"""

    model_config = ConfigDict(extra="forbid")

    result_field: str = Field(description="最终 SQL 结果中的字段名或别名")
    source_table: AllowedTableName = Field(description="数据库注释所属的真实表名")
    source_field: str = Field(description="数据库注释所属的真实字段名")
    comment: str = Field(description="从 information_schema 读取的原始字段注释")


class ResultTranslationSubgraphResult(BaseModel):
    """翻译子图的目标、注释事实、规则和可安全降级的完整翻译结果。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "failure"] = "success"
    targets: list[TranslationTarget] = Field(default_factory=list)
    comment_facts: list[ColumnCommentFact] = Field(default_factory=list)
    rules: list[ColumnTranslationRule] = Field(default_factory=list)
    original_rows: list[dict[str, Any]] = Field(default_factory=list)
    translated_rows: list[dict[str, Any]] = Field(default_factory=list)
    target_detection_raw_responses: list[str] = Field(default_factory=list)
    rule_generation_raw_responses: list[str] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description="翻译失败时的内部错误摘要；正式 HTTP 响应不会直接暴露",
    )


class TranslationToolValidationError(ValueError):
    """携带稳定错误码、字段路径和唯一修复动作的翻译工具语义校验异常。"""

    # 保存可直接回传模型的精确错误事实，避免重试依赖模糊异常文本猜测修复方向。
    def __init__(
        self,
        code: str,
        field_path: str,
        message: str,
        repair_action: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field_path = field_path
        self.repair_action = repair_action


class _SchemaField(BaseModel):
    """从紧凑结构响应恢复的内部字段事实，不直接暴露给模型工具参数。"""

    field_name: str
    data_type: str
    foreign_key: str | None
    comment: str | None


class _ResultTranslationState(TypedDict, total=False):
    """三个翻译节点间只传递 SQL、结构、受限样本、规则与完整原始行。"""

    sql: str
    result_columns: list[str]
    rows: list[dict[str, Any]]
    provided_schema_results: list[TableSchemaToolResponse] | None
    schema_results: list[TableSchemaToolResponse]
    schema_fields: dict[str, dict[str, _SchemaField]]
    direct_lineage: dict[str, tuple[str, str]]
    targets: list[TranslationTarget]
    comment_facts: list[ColumnCommentFact]
    rules: list[ColumnTranslationRule]
    translated_rows: list[dict[str, Any]]
    target_detection_raw_responses: list[str]
    rule_generation_raw_responses: list[str]


# 将 OpenAI 兼容响应完整序列化，便于实验回放字段识别和注释解析行为。
def _serialize_raw_response(response: Any) -> str:
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    return repr(response)


# 从既有紧凑表结构结果恢复字段、类型、外键与注释，避免实验改动公共工具响应契约。
def _parse_schema_fields(
    schema_result: TableSchemaToolResponse,
) -> dict[str, _SchemaField]:
    if schema_result.status != "success" or schema_result.table_name is None:
        raise RuntimeError(
            f"表 {schema_result.table_name or '-'} 的结构读取失败：{schema_result.result}"
        )
    parsed_result = parse_yaml_context(schema_result.result)
    if not isinstance(parsed_result, dict):
        raise RuntimeError(f"表 {schema_result.table_name} 的 YAML 结构不是对象")
    if parsed_result.get("table") != schema_result.table_name:
        raise RuntimeError(f"表 {schema_result.table_name} 的 YAML 表名不一致")
    raw_columns = parsed_result.get("columns")
    if not isinstance(raw_columns, list):
        raise RuntimeError(f"表 {schema_result.table_name} 的 YAML 缺少 columns 列表")
    fields: dict[str, _SchemaField] = {}
    for raw_column in raw_columns:
        field = _SchemaField.model_validate(raw_column)
        fields[field.field_name] = field
    if not fields:
        raise RuntimeError(f"表 {schema_result.table_name} 的结构结果未包含可解析字段")
    return fields


# 解析 SQL 中的真实表和别名，节点 1 只允许为直接返回的原始列建立来源关系。
def _extract_direct_result_lineage(sql: str) -> tuple[list[str], dict[str, tuple[str, str]]]:
    expression = parse_one(sql, dialect="mysql")
    top_level_select = expression if isinstance(expression, exp.Select) else None
    cte_names = {
        cte.alias_or_name
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    table_names: list[str] = []
    top_level_alias_to_table: dict[str, str] = {}
    for table in expression.find_all(exp.Table):
        table_name = table.name
        if table_name in cte_names:
            continue
        if table_name not in table_names:
            table_names.append(table_name)
        if (
            top_level_select is not None
            and table.find_ancestor(exp.Select) is top_level_select
        ):
            top_level_alias_to_table[table.alias_or_name] = table_name
            top_level_alias_to_table[table_name] = table_name

    direct_lineage: dict[str, tuple[str, str]] = {}
    if top_level_select is None:
        return table_names, direct_lineage
    unique_top_level_tables = set(top_level_alias_to_table.values())
    for selection in top_level_select.selects:
        output_name = selection.alias_or_name
        selected_expression = selection.this if isinstance(selection, exp.Alias) else selection
        if not output_name or not isinstance(selected_expression, exp.Column):
            continue
        source_table = top_level_alias_to_table.get(selected_expression.table)
        if (
            source_table is None
            and not selected_expression.table
            and len(unique_top_level_tables) == 1
        ):
            source_table = next(iter(unique_top_level_tables))
        if source_table is not None:
            direct_lineage[output_name] = (source_table, selected_expression.name)
    return table_names, direct_lineage


# 渲染不含注释的结构上下文，让节点 1 只判断字段类型和来源，避免提前承担注释解析职责。
def _render_schema_without_comments(
    schema_fields: dict[str, dict[str, _SchemaField]],
) -> dict[str, list[dict[str, Any]]]:
    tables: list[dict[str, Any]] = []
    for table_name, fields in schema_fields.items():
        tables.append(
            {
                "table": table_name,
                "columns": [
                    {
                        "field_name": field.field_name,
                        "data_type": field.data_type,
                        "foreign_key": field.foreign_key,
                    }
                    for field in fields.values()
                ],
            }
        )
    return {"tables": tables}


# 将业务域白名单写入嵌套工具模型的来源表字段，远端 Schema 与本地权限使用相同有序集合。
def _apply_source_table_enum(
    definition: dict[str, object],
    nested_model_name: str,
    allowed_tables: Collection[str],
) -> dict[str, object]:
    function = definition["function"]
    assert isinstance(function, dict)
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    nested_models = parameters["$def"]
    assert isinstance(nested_models, dict)
    nested_model = nested_models[nested_model_name]
    assert isinstance(nested_model, dict)
    properties = nested_model["properties"]
    assert isinstance(properties, dict)
    source_table = properties["source_table"]
    assert isinstance(source_table, dict)
    source_table["enum"] = list(allowed_tables)
    return definition


# 构造节点 1 的 strict 工具定义，模型只能从当前业务域表白名单提交封闭的翻译目标列表。
def build_translation_targets_tool_definition(
    allowed_tables: Collection[str],
) -> dict[str, object]:
    definition = build_strict_tool_definition(
        SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
        "提交需要把数据库编码翻译成人类可读含义的结果字段及其直接来源；没有时提交空数组。",
        TranslationTargetSubmission,
    )
    return _apply_source_table_enum(
        definition,
        "TranslationTarget",
        allowed_tables,
    )


# 构造节点 2 的 strict 工具定义，来源表继续受业务域白名单约束且映射使用封闭对象数组。
def build_translation_rules_tool_definition(
    allowed_tables: Collection[str],
) -> dict[str, object]:
    definition = build_strict_tool_definition(
        SUBMIT_TRANSLATION_RULES_TOOL_NAME,
        "仅根据系统提供的字段 comment 提交原始值到展示含义的翻译规则。",
        TranslationRuleSubmission,
    )
    return _apply_source_table_enum(
        definition,
        "ColumnTranslationRule",
        allowed_tables,
    )


# 将工具协议或语义校验错误作为同一 tool_call_id 的正常失败结果返回，允许模型有限修复。
def _build_semantic_tool_error_message(
    tool_call_id: str,
    tool_name: str,
    error: TranslationToolValidationError,
) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(
            {
                "status": "failure",
                "error": {
                    "code": error.code,
                    "tool_name": tool_name,
                    "message": "工具参数未通过翻译层语义校验，本次工具调用未执行。",
                    "details": [
                        {
                            "error_type": "business_rule_violation",
                            "field_path": error.field_path,
                            "message": str(error),
                            "repair_action": error.repair_action,
                        }
                    ],
                },
                "retryable": True,
                "next_action": (
                    f"执行 details 中唯一的 repair_action，然后重新调用 {tool_name}。"
                ),
            },
            ensure_ascii=False,
        ),
    }


# 在模型没有产生可回应的 tool_call_id 时追加协议校验反馈，下一轮仍强制调用唯一工具。
def _build_protocol_repair_message(
    tool_name: str,
    error: TranslationToolValidationError,
) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "系统工具协议校验反馈（不是新的用户需求）："
            f"错误代码 {error.code}；字段路径 {error.field_path}；"
            f"错误原因：{error}；修复动作：{error.repair_action} "
            f"请重新且仅调用一次 {tool_name}。"
        ),
    }


# 抽取并校验模型必须且只能调用的指定工具，返回调用 ID 与已通过 Pydantic 的参数。
def _parse_forced_tool_call(
    message: Any,
    tool_name: str,
    arguments_model: type[BaseModel],
) -> tuple[str, BaseModel]:
    tool_calls = getattr(message, "tool_calls", None) or []
    if len(tool_calls) != 1:
        raise TranslationToolValidationError(
            code="invalid_tool_call_count",
            field_path="$",
            message=f"模型必须且只能调用一次 {tool_name}，实际调用 {len(tool_calls)} 次",
            repair_action=f"仅调用一次 {tool_name}，不要输出普通文本或并行调用其他工具。",
        )
    tool_call = tool_calls[0]
    if tool_call.function.name != tool_name:
        raise TranslationToolValidationError(
            code="unexpected_tool_name",
            field_path="$",
            message=f"模型调用了未注册工具 {tool_call.function.name}，当前只允许 {tool_name}",
            repair_action=f"将工具调用改为 {tool_name}，并按该工具 Schema 重新提交完整参数。",
        )
    return (
        tool_call.id,
        arguments_model.model_validate_json(tool_call.function.arguments),
    )


# 将数据库标量统一为注释映射键；未知值不会被猜测或隐式转型为其他状态。
def _normalize_mapping_key(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class ResultTranslationSubgraph:
    """以两个受约束模型节点和一个确定性程序节点翻译查询结果中的状态编码。"""

    # 初始化模型、结构缓存与固定三节点工作流，完整 SQL 结果只在程序翻译节点遍历一次。
    def __init__(
        self,
        client: Any,
        model: str,
        schema_reader: SchemaReader,
        allowed_tables: Collection[str],
        max_tokens: int = DEFAULT_TRANSLATION_MAX_TOKENS,
        max_parallel_fields: int = MAX_PARALLEL_TRANSLATION_FIELD_COUNT,
        trace_writer: Callable[[str], None] | None = None,
    ) -> None:
        if not allowed_tables:
            raise ValueError("翻译子图至少需要一张允许表")
        if max_parallel_fields < 1:
            raise ValueError("翻译规则并行数必须大于零")
        self._client = client
        self._model = model
        self._schema_reader = schema_reader
        self._allowed_tables = tuple(dict.fromkeys(allowed_tables))
        self._allowed_table_names = frozenset(self._allowed_tables)
        self._max_tokens = max_tokens
        self._max_parallel_fields = max_parallel_fields
        self._trace_writer = trace_writer

        workflow = StateGraph(_ResultTranslationState)
        workflow.add_node("identify_translation_targets", self._identify_translation_targets)
        workflow.add_node("build_translation_rules", self._build_translation_rules)
        workflow.add_node("apply_translations", self._apply_translations)
        workflow.add_edge(START, "identify_translation_targets")
        workflow.add_edge("identify_translation_targets", "build_translation_rules")
        workflow.add_edge("build_translation_rules", "apply_translations")
        workflow.add_edge("apply_translations", END)
        self._workflow = workflow.compile()

    # 从业务域与项目配置创建 DeepSeek 客户端和白名单结构读取器，正式链路可注入进程级共享缓存。
    @classmethod
    def from_settings(
        cls,
        domain_profile: QueryDomainProfile,
        settings: Settings | None = None,
        schema_reader: SchemaReader | None = None,
        trace_writer: Callable[[str], None] | None = None,
    ) -> "ResultTranslationSubgraph":
        resolved_settings = settings or get_settings()
        if resolved_settings.deepseek_api_key is None:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法执行查询结果翻译")
        client = OpenAI(
            api_key=resolved_settings.deepseek_api_key.get_secret_value(),
            base_url=build_strict_tools_base_url(str(resolved_settings.deepseek_base_url)),
            timeout=resolved_settings.deepseek_http_timeout_seconds,
        )
        cached_reader = schema_reader or CachingTableSchemaReader(
            InformationSchemaTableSchemaReader(
                resolved_settings,
                domain_profile.allowed_tables,
            ).read
        ).read
        return cls(
            client=client,
            model=resolved_settings.deepseek_model,
            schema_reader=cached_reader,
            allowed_tables=domain_profile.allowed_tables,
            max_tokens=resolved_settings.deepseek_query_translation_max_tokens,
            max_parallel_fields=(
                resolved_settings.agent_query_translation_max_parallel_fields
            ),
            trace_writer=trace_writer,
        )

    # 输出每个模型节点的轮次和工具参数，便于 IDE 实验直接观察字段识别及规则提取。
    def _write_trace(self, content: str) -> None:
        if self._trace_writer is not None:
            self._trace_writer(content)

    # 优先复用上游已读取结构；缺失时只为 SQL 实际涉及表补读，并建立字段事实索引。
    def _load_schema_context(
        self,
        table_names: list[str],
        provided_schema_results: list[TableSchemaToolResponse] | None,
    ) -> tuple[list[TableSchemaToolResponse], dict[str, dict[str, _SchemaField]]]:
        provided_by_table = {
            result.table_name: result
            for result in provided_schema_results or []
            if result.table_name is not None and result.status == "success"
        }
        schema_results: list[TableSchemaToolResponse] = []
        schema_fields: dict[str, dict[str, _SchemaField]] = {}
        for table_name in table_names:
            if table_name not in self._allowed_table_names:
                raise ValueError(f"SQL 涉及不允许的表：{table_name}")
            schema_result = provided_by_table.get(table_name) or self._schema_reader(
                table_name
            )
            schema_results.append(schema_result)
            schema_fields[table_name] = _parse_schema_fields(schema_result)
        return schema_results, schema_fields

    # 校验节点 1 目标严格对应 SQL 直接返回列，禁止模型猜测表达式、别名或跨表字段来源。
    def _validate_targets(
        self,
        submission: TranslationTargetSubmission,
        result_columns: list[str],
        direct_lineage: dict[str, tuple[str, str]],
        schema_fields: dict[str, dict[str, _SchemaField]],
    ) -> None:
        seen_fields: set[str] = set()
        for target_index, target in enumerate(submission.targets):
            target_path = f"targets.{target_index}"
            if target.result_field in seen_fields:
                raise TranslationToolValidationError(
                    code="duplicate_translation_target",
                    field_path=f"{target_path}.result_field",
                    message=f"结果字段 {target.result_field} 被重复提交。",
                    repair_action=f"删除重复的 {target_path}，只保留该结果字段第一次出现的目标项。",
                )
            seen_fields.add(target.result_field)
            if target.result_field not in result_columns:
                raise TranslationToolValidationError(
                    code="unknown_result_field",
                    field_path=f"{target_path}.result_field",
                    message=f"结果字段 {target.result_field} 不属于 SQL 返回列。",
                    repair_action=f"删除 {target_path}；只能从 result_columns 中选择待翻译字段。",
                )
            expected_source = direct_lineage.get(target.result_field)
            actual_source = (target.source_table, target.source_field)
            if expected_source != actual_source:
                if expected_source is None:
                    raise TranslationToolValidationError(
                        code="non_direct_result_expression",
                        field_path=target_path,
                        message=(
                            f"结果字段 {target.result_field} 不是可追溯的直接原始列，"
                            "不能进入翻译层。"
                        ),
                        repair_action=f"删除 {target_path}。",
                    )
                raise TranslationToolValidationError(
                    code="result_field_lineage_mismatch",
                    field_path=target_path,
                    message=(
                        f"结果字段 {target.result_field} 的直接来源是 "
                        f"{expected_source[0]}.{expected_source[1]}，实际提交 "
                        f"{actual_source[0]}.{actual_source[1]}。"
                    ),
                    repair_action=(
                        f"将 {target_path}.source_table 改为 {expected_source[0]}，"
                        f"并将 {target_path}.source_field 改为 {expected_source[1]}。"
                    ),
                )
            if target.source_field not in schema_fields.get(target.source_table, {}):
                raise TranslationToolValidationError(
                    code="source_field_not_found",
                    field_path=f"{target_path}.source_field",
                    message=(
                        f"字段 {target.source_table}.{target.source_field} "
                        "不存在于已读取表结构。"
                    ),
                    repair_action=f"删除 {target_path}。",
                )

    # 节点 1 依据无注释结构、SQL 直接来源提示和前五行样本识别需要翻译的编码字段。
    def _identify_translation_targets(
        self,
        state: _ResultTranslationState,
    ) -> dict[str, Any]:
        table_names, direct_lineage = _extract_direct_result_lineage(state["sql"])
        schema_results, schema_fields = self._load_schema_context(
            table_names,
            state.get("provided_schema_results"),
        )
        context = {
            "executed_sql": state["sql"],
            "result_columns": state["result_columns"],
            "direct_column_lineage": {
                result_field: {
                    "source_table": source[0],
                    "source_field": source[1],
                }
                for result_field, source in direct_lineage.items()
            },
            "table_schemas_without_comments": _render_schema_without_comments(
                schema_fields
            ),
            "rows_preview": state["rows"][:RESULT_PREVIEW_ROW_COUNT],
        }
        messages: list[Any] = [
            {
                "role": "system",
                "content": TRANSLATION_TARGET_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": render_yaml_context(context),
            },
        ]
        tool = build_translation_targets_tool_definition(self._allowed_tables)
        raw_responses: list[str] = []
        last_error: Exception | None = None
        for attempt in range(MAX_TOOL_ARGUMENT_REPAIR_COUNT + 1):
            self._write_trace(
                f"[翻译层节点 1] 第 {attempt + 1} 次模型调用：识别待翻译字段"
            )
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=[tool],
                tool_choice={
                    "type": "function",
                    "function": {"name": SUBMIT_TRANSLATION_TARGETS_TOOL_NAME},
                },
                **build_non_thinking_completion_options(self._max_tokens),
            )
            raw_responses.append(_serialize_raw_response(response))
            message = response.choices[0].message
            try:
                tool_call_id, parsed = _parse_forced_tool_call(
                    message,
                    SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                    TranslationTargetSubmission,
                )
                assert isinstance(parsed, TranslationTargetSubmission)
                self._validate_targets(
                    parsed,
                    state["result_columns"],
                    direct_lineage,
                    schema_fields,
                )
                self._write_trace(
                    "[翻译层节点 1] 已识别字段："
                    + (", ".join(item.result_field for item in parsed.targets) or "无")
                )
                return {
                    "schema_results": schema_results,
                    "schema_fields": schema_fields,
                    "direct_lineage": direct_lineage,
                    "targets": parsed.targets,
                    "target_detection_raw_responses": raw_responses,
                }
            except (ValidationError, TranslationToolValidationError) as error:
                last_error = error
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                messages.append(message)
                tool_calls = getattr(message, "tool_calls", None) or []
                if not tool_calls:
                    assert isinstance(error, TranslationToolValidationError)
                    messages.append(
                        _build_protocol_repair_message(
                            SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                            error,
                        )
                    )
                    continue
                if isinstance(error, ValidationError):
                    messages.append(
                        build_tool_argument_error_message(
                            tool_calls[0].id,
                            SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                            error,
                        )
                    )
                else:
                    messages.extend(
                        _build_semantic_tool_error_message(
                            tool_call.id,
                            SUBMIT_TRANSLATION_TARGETS_TOOL_NAME,
                            error,
                        )
                        for tool_call in tool_calls
                    )
        raise RuntimeError(f"翻译目标识别失败：{last_error}")

    # 从节点 1 精确目标中单独提取 comment，禁止把其他字段注释带入规则生成上下文。
    def _build_comment_facts(
        self,
        targets: list[TranslationTarget],
        schema_fields: dict[str, dict[str, _SchemaField]],
    ) -> list[ColumnCommentFact]:
        facts: list[ColumnCommentFact] = []
        for target in targets:
            field = schema_fields[target.source_table][target.source_field]
            if field.comment:
                facts.append(
                    ColumnCommentFact(
                        result_field=target.result_field,
                        source_table=target.source_table,
                        source_field=target.source_field,
                        comment=field.comment,
                    )
                )
        return facts

    # 校验节点 2 的每条映射都能在原始 comment 中找到，未知或模型外推含义不得进入程序翻译。
    def _validate_rules(
        self,
        submission: TranslationRuleSubmission,
        targets: list[TranslationTarget],
        comment_facts: list[ColumnCommentFact],
    ) -> None:
        target_by_field = {target.result_field: target for target in targets}
        comment_by_field = {fact.result_field: fact.comment for fact in comment_facts}
        seen_fields: set[str] = set()
        for rule_index, rule in enumerate(submission.translations):
            rule_path = f"translations.{rule_index}"
            target = target_by_field.get(rule.result_field)
            if target is None:
                raise TranslationToolValidationError(
                    code="rule_target_not_identified",
                    field_path=f"{rule_path}.result_field",
                    message=f"翻译规则字段 {rule.result_field} 未经节点 1 识别。",
                    repair_action=f"删除 {rule_path}；只能为 column_comments 中的结果字段生成规则。",
                )
            if rule.result_field in seen_fields:
                raise TranslationToolValidationError(
                    code="duplicate_translation_rule",
                    field_path=f"{rule_path}.result_field",
                    message=f"翻译规则字段 {rule.result_field} 被重复提交。",
                    repair_action=f"删除重复的 {rule_path}，只保留该字段第一次出现的规则。",
                )
            seen_fields.add(rule.result_field)
            if (rule.source_table, rule.source_field) != (
                target.source_table,
                target.source_field,
            ):
                raise TranslationToolValidationError(
                    code="translation_rule_lineage_mismatch",
                    field_path=rule_path,
                    message=(
                        f"翻译规则字段 {rule.result_field} 的来源必须是 "
                        f"{target.source_table}.{target.source_field}。"
                    ),
                    repair_action=(
                        f"将 {rule_path}.source_table 改为 {target.source_table}，"
                        f"并将 {rule_path}.source_field 改为 {target.source_field}。"
                    ),
                )
            if not rule.mappings:
                raise TranslationToolValidationError(
                    code="empty_translation_mappings",
                    field_path=f"{rule_path}.mappings",
                    message=f"翻译规则字段 {rule.result_field} 未提供任何映射。",
                    repair_action=(
                        f"从该字段 comment 中逐项提取映射并写入 {rule_path}.mappings；"
                        f"若无法可靠提取，则删除 {rule_path}。"
                    ),
                )
            comment = comment_by_field.get(rule.result_field)
            if not comment:
                raise TranslationToolValidationError(
                    code="missing_column_comment",
                    field_path=rule_path,
                    message=f"字段 {rule.result_field} 没有可用 comment。",
                    repair_action=f"删除 {rule_path}。",
                )
            raw_values: set[str] = set()
            for mapping_index, mapping in enumerate(rule.mappings):
                mapping_path = f"{rule_path}.mappings.{mapping_index}"
                if mapping.raw_value in raw_values:
                    raise TranslationToolValidationError(
                        code="duplicate_raw_mapping_value",
                        field_path=f"{mapping_path}.raw_value",
                        message=(
                            f"字段 {rule.result_field} 的原始值 "
                            f"{mapping.raw_value} 被重复定义。"
                        ),
                        repair_action=f"删除重复的 {mapping_path}。",
                    )
                raw_values.add(mapping.raw_value)
                if mapping.raw_value not in comment:
                    raise TranslationToolValidationError(
                        code="raw_value_not_in_comment",
                        field_path=f"{mapping_path}.raw_value",
                        message=(
                            f"原始值 {mapping.raw_value} 未出现在字段 "
                            f"{rule.result_field} 的 comment 中。"
                        ),
                        repair_action=f"删除 {mapping_path}；不得改写或猜测 comment 未定义的原始值。",
                    )
                if mapping.display_value not in comment:
                    raise TranslationToolValidationError(
                        code="display_value_not_in_comment",
                        field_path=f"{mapping_path}.display_value",
                        message=(
                            f"展示值 {mapping.display_value} 未出现在字段 "
                            f"{rule.result_field} 的 comment 中。"
                        ),
                        repair_action=f"删除 {mapping_path}；不得改写或猜测 comment 未定义的展示值。",
                    )

    # 为单个字段构造独立 YAML 并有限修复其规则调用，避免其他字段注释污染当前映射。
    def _build_translation_rule_for_field(
        self,
        target: TranslationTarget,
        comment_fact: ColumnCommentFact,
        rows: list[dict[str, Any]],
    ) -> tuple[list[ColumnTranslationRule], list[str]]:
        context = {
            "column_comments": [comment_fact.model_dump()],
            "observed_values": {
                target.result_field: list(
                    dict.fromkeys(
                        _normalize_mapping_key(row.get(target.result_field))
                        for row in rows[:RESULT_PREVIEW_ROW_COUNT]
                    )
                )
            },
        }
        messages: list[Any] = [
            {"role": "system", "content": TRANSLATION_RULE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": render_yaml_context(context),
            },
        ]
        tool = build_translation_rules_tool_definition(self._allowed_tables)
        raw_responses: list[str] = []
        last_error: Exception | None = None
        for attempt in range(MAX_TOOL_ARGUMENT_REPAIR_COUNT + 1):
            self._write_trace(
                f"[翻译层节点 2][{target.result_field}] "
                f"第 {attempt + 1} 次模型调用：解析字段 comment"
            )
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                tools=[tool],
                tool_choice={
                    "type": "function",
                    "function": {"name": SUBMIT_TRANSLATION_RULES_TOOL_NAME},
                },
                **build_non_thinking_completion_options(self._max_tokens),
            )
            raw_responses.append(_serialize_raw_response(response))
            message = response.choices[0].message
            try:
                _, parsed = _parse_forced_tool_call(
                    message,
                    SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                    TranslationRuleSubmission,
                )
                assert isinstance(parsed, TranslationRuleSubmission)
                self._validate_rules(parsed, [target], [comment_fact])
                self._write_trace(
                    f"[翻译层节点 2][{target.result_field}] 已生成规则："
                    + (", ".join(item.result_field for item in parsed.translations) or "无")
                )
                return parsed.translations, raw_responses
            except (ValidationError, TranslationToolValidationError) as error:
                last_error = error
                if attempt >= MAX_TOOL_ARGUMENT_REPAIR_COUNT:
                    break
                messages.append(message)
                tool_calls = getattr(message, "tool_calls", None) or []
                if not tool_calls:
                    assert isinstance(error, TranslationToolValidationError)
                    messages.append(
                        _build_protocol_repair_message(
                            SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                            error,
                        )
                    )
                    continue
                if isinstance(error, ValidationError):
                    messages.append(
                        build_tool_argument_error_message(
                            tool_calls[0].id,
                            SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                            error,
                        )
                    )
                else:
                    messages.extend(
                        _build_semantic_tool_error_message(
                            tool_call.id,
                            SUBMIT_TRANSLATION_RULES_TOOL_NAME,
                            error,
                        )
                        for tool_call in tool_calls
                    )
        raise RuntimeError(
            f"字段 {target.result_field} 的 comment 翻译规则生成失败：{last_error}"
        )

    # 节点 2 为每个字段单独解析 comment，并以受控线程数并行调用相同固定前缀以复用缓存。
    def _build_translation_rules(
        self,
        state: _ResultTranslationState,
    ) -> dict[str, Any]:
        comment_facts = self._build_comment_facts(
            state["targets"],
            state["schema_fields"],
        )
        if not comment_facts:
            self._write_trace("[翻译层节点 2] 没有可解析 comment，跳过模型调用")
            return {
                "comment_facts": [],
                "rules": [],
                "rule_generation_raw_responses": [],
            }

        targets_by_field = {
            target.result_field: target for target in state["targets"]
        }
        worker_arguments = [
            (targets_by_field[fact.result_field], fact, state["rows"])
            for fact in comment_facts
        ]
        max_workers = min(
            len(worker_arguments),
            self._max_parallel_fields,
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    self._build_translation_rule_for_field,
                    *arguments,
                )
                for arguments in worker_arguments
            ]
            generated_results = [future.result() for future in futures]

        rules = [
            rule
            for field_rules, _ in generated_results
            for rule in field_rules
        ]
        raw_responses = [
            raw_response
            for _, field_responses in generated_results
            for raw_response in field_responses
        ]
        return {
            "comment_facts": comment_facts,
            "rules": rules,
            "rule_generation_raw_responses": raw_responses,
        }

    # 节点 3 对完整结果逐行应用规则，未定义值、空值和非目标字段原样保留。
    def _apply_translations(
        self,
        state: _ResultTranslationState,
    ) -> dict[str, Any]:
        mapping_by_field = {
            rule.result_field: {
                mapping.raw_value: mapping.display_value for mapping in rule.mappings
            }
            for rule in state["rules"]
        }
        translated_rows: list[dict[str, Any]] = []
        for row in state["rows"]:
            translated_row = dict(row)
            for result_field, field_mapping in mapping_by_field.items():
                original_value = row.get(result_field)
                translated_row[result_field] = field_mapping.get(
                    _normalize_mapping_key(original_value),
                    original_value,
                )
            translated_rows.append(translated_row)
        self._write_trace(
            f"[翻译层节点 3] 已对 {len(translated_rows)} 行完整结果执行确定性翻译"
        )
        return {"translated_rows": translated_rows}

    # 运行独立翻译子图；空结果直接跳过，模型或结构异常时保留原始行供后续安全降级。
    def run(
        self,
        sql: str,
        result_columns: list[str],
        rows: list[dict[str, Any]],
        schema_results: list[TableSchemaToolResponse] | None = None,
    ) -> ResultTranslationSubgraphResult:
        if not sql.strip():
            raise ValueError("最终 SQL 不能为空")
        if not result_columns:
            raise ValueError("结果字段列表不能为空")
        if not rows:
            return ResultTranslationSubgraphResult(
                original_rows=[],
                translated_rows=[],
            )
        try:
            final_state = self._workflow.invoke(
                {
                    "sql": sql,
                    "result_columns": result_columns,
                    "rows": rows,
                    "provided_schema_results": schema_results,
                }
            )
        except Exception as error:
            self._write_trace(f"[翻译层] 执行失败，保留原始结果：{error}")
            return ResultTranslationSubgraphResult(
                status="failure",
                original_rows=rows,
                translated_rows=[dict(row) for row in rows],
                error=str(error)[:500],
            )
        return ResultTranslationSubgraphResult(
            targets=final_state["targets"],
            comment_facts=final_state["comment_facts"],
            rules=final_state["rules"],
            original_rows=rows,
            translated_rows=final_state["translated_rows"],
            target_detection_raw_responses=final_state[
                "target_detection_raw_responses"
            ],
            rule_generation_raw_responses=final_state[
                "rule_generation_raw_responses"
            ],
        )
