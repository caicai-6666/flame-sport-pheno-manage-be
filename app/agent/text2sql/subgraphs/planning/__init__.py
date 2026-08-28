"""装配并按需公开查询规划子图的稳定入口。"""

from typing import Any

__all__ = [
    "DeepSeekQueryPlanningAgent",
    "QueryPlanningAgentResult",
    "QueryPlanningExecutionError",
]


# 延迟加载规划节点，避免共享表结构组件导入工具模型时反向触发规划节点形成循环依赖。
def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.agent.text2sql.subgraphs.planning import node

    return getattr(node, name)
