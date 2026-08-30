"""将 Text-to-SQL 工作流动作转换为操作员能够理解的进度更新。"""

from collections.abc import Callable

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.agent.text2sql.events.models import AgentProgressUpdate


ProgressEmitter = Callable[[AgentProgressUpdate], None]


class AgentProgressReporter:
    """集中维护前台展示文案，避免各工具泄漏表名、SQL 和模型内部轨迹。"""

    # 保存业务域友好名称和事件出口；未注入出口时允许引擎独立测试而不产生副作用。
    def __init__(
        self,
        domain_profile: QueryDomainProfile,
        emitter: ProgressEmitter | None = None,
    ) -> None:
        self._domain_profile = domain_profile
        self._emitter = emitter

    # 发布经过长度约束的结构化进度，调用方不能直接输出模型原始响应或技术栈信息。
    def emit(self, update: AgentProgressUpdate) -> None:
        if self._emitter is not None:
            self._emitter(update)

    # 把表结构读取转换为业务对象说明，使操作员知道系统正在准备哪类数据。
    def schema_lookup_started(self, table_name: str) -> None:
        resource_name = self._domain_profile.table_labels.get(table_name, "业务数据")
        self.emit(
            AgentProgressUpdate(
                stage="planning",
                event_type="progress_updated",
                status="running",
                title="正在准备查询数据",
                message=f"正在了解{resource_name}的数据结构。",
            )
        )

    # 把单表候选检索转换为业务实体核对提示，不暴露内部 SQL 和字段实现。
    def entity_lookup_started(self, table_name: str) -> None:
        resource_name = self._domain_profile.table_labels.get(table_name, "业务信息")
        self.emit(
            AgentProgressUpdate(
                stage="planning",
                event_type="progress_updated",
                status="running",
                title="正在核对查询条件",
                message=f"正在核对用户提到的{resource_name}。",
            )
        )

    # 在模型记录关键判断时只展示动作摘要，原始推理继续留在受限内部诊断轨迹。
    def reasoning_progress(self, stage: str) -> None:
        message = (
            "正在确认业务词汇和查询范围。"
            if stage == "alignment"
            else "正在确定需要的数据和查询口径。"
        )
        self.emit(
            AgentProgressUpdate(
                stage=stage,
                event_type="progress_updated",
                status="running",
                title="正在分析查询需求",
                message=message,
            )
        )

    # 用户提出字段修改意见后发布重新规划状态，但不把可能敏感的自由文本反馈写入 SSE。
    def plan_revision_started(self) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="planning",
                event_type="progress_updated",
                status="running",
                title="正在调整查询方案",
                message="正在根据您对结果字段的意见调整查询方案。",
            )
        )

    # 在规划模型调用原料查询工具前发布 SQL 生成阶段，确保完成事件之前一定存在对应开始事件。
    def material_query_started(self) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="sql_generation",
                event_type="stage_started",
                status="running",
                title="正在生成安全查询",
                message="正在把原料要求转换为只读查询并执行安全检查。",
            )
        )

    # 原料读取成功后报告完整结果行数，模型仅观察受限预览而完整数据留在后台。
    def material_query_completed(self, row_count: int) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="execution",
                event_type="stage_completed",
                status="success",
                title="数据读取完成",
                message=f"共读取到 {row_count} 条原料，正在核对是否满足查询需求。",
            )
        )

    # 原料查询的单轮失败仍允许规划模型修正重试，因此只发布运行中进度，避免前端误判为查询终态。
    def material_query_failed(self) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="execution",
                event_type="progress_updated",
                status="running",
                title="本轮数据读取未完成",
                message="正在根据安全错误提示调整查询要求。",
            )
        )

    # 在规划模型调用塑形工具前发布真实阶段进度，避免 Pipeline 事后补发已完成动作。
    def material_shaping_started(self) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="shaping",
                event_type="stage_started",
                status="running",
                title="正在整理表格结构",
                message="正在基于完整原料按目标行列布局生成表格。",
            )
        )

    # 塑形成功后报告输入与输出规模，前台无需接触内部结果 ID 或模型工具参数。
    def material_shaping_completed(
        self,
        source_row_count: int,
        result_row_count: int,
    ) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="shaping",
                event_type="stage_completed",
                status="success",
                title="表格结构整理完成",
                message=(
                    f"已将 {source_row_count} 条原料整理为 "
                    f"{result_row_count} 行结果，正在核对表格。"
                ),
            )
        )

    # 塑形的单轮失败仍允许规划模型修正重试，因此只发布运行中进度，真正终止统一由查询终态事件表达。
    def material_shaping_failed(self) -> None:
        self.emit(
            AgentProgressUpdate(
                stage="shaping",
                event_type="progress_updated",
                status="running",
                title="本轮表格整理未完成",
                message="正在根据结果布局反馈调整整理方式。",
            )
        )
