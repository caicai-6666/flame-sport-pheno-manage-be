"""将通用工作流动作转换为操作员能够理解的进度更新。"""

from collections.abc import Callable

from app.agent.domains.base import QueryDomainProfile
from app.agent.events.models import AgentProgressUpdate


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
