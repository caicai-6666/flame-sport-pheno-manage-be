"""查询业务域配置与注册入口。"""

from app.agent.domains.base import QueryDomainProfile
from app.agent.domains.registry import get_query_domain_profile

__all__ = ["QueryDomainProfile", "get_query_domain_profile"]
