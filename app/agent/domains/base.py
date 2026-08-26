"""定义查询智能体可注入的业务域配置。"""

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping


@dataclass(frozen=True, slots=True)
class QueryPlanPolicyIssue:
    """描述业务域对查询计划的一项可修复违规。"""

    field_path: str
    message: str
    repair_action: str


@dataclass(frozen=True, slots=True)
class AlignmentPolicyIssue:
    """描述业务对齐结果的一项可修复业务交付违规。"""

    field_path: str
    message: str
    repair_action: str


QueryPlanValidator = Callable[
    [str, object, object | None],
    tuple[QueryPlanPolicyIssue, ...],
]
AlignmentValidator = Callable[
    [str, str, tuple[str, ...]],
    tuple[AlignmentPolicyIssue, ...],
]


@dataclass(frozen=True, slots=True)
class QueryDomainProfile:
    """集中声明一个查询业务域的知识文件、数据权限和友好展示名称。"""

    key: str
    display_name: str
    query_scope: str
    root_directory: Path
    allowed_tables: tuple[str, ...]
    table_context_files: tuple[str, ...]
    table_labels: Mapping[str, str]
    protected_database_identifiers: frozenset[str]
    query_plan_validator: QueryPlanValidator | None = None
    alignment_validator: AlignmentValidator | None = None
    alignment_prompt_instructions: str = ""
    planning_prompt_instructions: str = ""

    # 返回业务对齐层无关系表概述目录，避免各阶段自行推导资源路径。
    @property
    def alignment_context_directory(self) -> Path:
        return self.root_directory / "business-alignment"

    # 返回规划层轻量表概述目录，文件顺序由业务域显式维护。
    @property
    def table_context_directory(self) -> Path:
        return self.root_directory / "table-context"

    # 返回对齐与规划共享的核心业务规则文件。
    @property
    def core_rules_path(self) -> Path:
        return self.root_directory / "business-context" / "core-game-rules.txt"

    # 返回声明式实体检索配置文件，工具实现不再绑定具体业务域路径。
    @property
    def entity_lookup_config_path(self) -> Path:
        return self.root_directory / "entity-lookup.json"

    # 在应用启动或测试装配时完整验证业务包，避免运行到模型中途才发现缺失配置。
    def validate_resources(self) -> None:
        if not self.key.strip():
            raise ValueError("业务域 key 不能为空")
        if not self.allowed_tables:
            raise ValueError(f"业务域 {self.key} 至少需要允许一张表")
        if len(self.allowed_tables) != len(set(self.allowed_tables)):
            raise ValueError(f"业务域 {self.key} 的 allowed_tables 不能重复")
        if len(self.table_context_files) != len(self.allowed_tables):
            raise ValueError(
                f"业务域 {self.key} 必须为每张 allowed_tables 表提供一个概述文件"
            )
        if len(self.table_context_files) != len(set(self.table_context_files)):
            raise ValueError(f"业务域 {self.key} 的 table_context_files 不能重复")
        if set(self.table_labels) != set(self.allowed_tables):
            raise ValueError(
                f"业务域 {self.key} 的 table_labels 必须完整覆盖 allowed_tables"
            )
        if not self.protected_database_identifiers:
            raise ValueError(
                f"业务域 {self.key} 必须声明业务对齐层禁止输出的数据库标识符"
            )

        required_files = [
            self.alignment_context_directory / "table-overview.txt",
            self.alignment_context_directory / "business-vocabulary.txt",
            self.core_rules_path,
            self.entity_lookup_config_path,
            *(
                self.table_context_directory / filename
                for filename in self.table_context_files
            ),
        ]
        missing_files = [str(path) for path in required_files if not path.is_file()]
        if missing_files:
            raise FileNotFoundError(
                f"业务域 {self.key} 缺少配置文件：" + "、".join(missing_files)
            )

    # 在终止工具成功解析后联合校验数据获取与结果塑形契约，无自定义校验器时直接通过。
    def validate_query_plan(
        self,
        planning_input: str,
        query_plan: object,
        result_shape_plan: object | None = None,
    ) -> tuple[QueryPlanPolicyIssue, ...]:
        if self.query_plan_validator is None:
            return ()
        return self.query_plan_validator(
            planning_input,
            query_plan,
            result_shape_plan,
        )

    # 在业务对齐终止工具成功解析后检查领域交付约束，避免语义完整但无法落地的需求进入规划层。
    def validate_alignment(
        self,
        original_question: str,
        aligned_question: str,
        business_constraints: tuple[str, ...],
    ) -> tuple[AlignmentPolicyIssue, ...]:
        if self.alignment_validator is None:
            return ()
        return self.alignment_validator(
            original_question,
            aligned_question,
            business_constraints,
        )


# 冻结展示名称映射，防止进程运行期间被请求或测试意外修改。
def freeze_table_labels(labels: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(labels))
