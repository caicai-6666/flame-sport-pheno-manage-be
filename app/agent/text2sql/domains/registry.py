"""维护 Text-to-SQL 查询业务域的显式注册表。"""

from app.agent.text2sql.domains.base import QueryDomainProfile
from app.agent.text2sql.domains.rewards.profile import REWARDS_QUERY_PROFILE
from app.agent.text2sql.domains.sports.profile import SPORTS_QUERY_PROFILE


QUERY_DOMAIN_REGISTRY: dict[str, QueryDomainProfile] = {
    SPORTS_QUERY_PROFILE.key: SPORTS_QUERY_PROFILE,
    REWARDS_QUERY_PROFILE.key: REWARDS_QUERY_PROFILE,
}


# 仅允许从代码注册表选择业务域，拒绝把请求值解释为文件路径或动态导入目标。
def get_query_domain_profile(domain_key: str) -> QueryDomainProfile:
    profile = QUERY_DOMAIN_REGISTRY.get(domain_key)
    if profile is None:
        raise KeyError(f"不支持的查询业务域：{domain_key}")
    profile.validate_resources()
    return profile
