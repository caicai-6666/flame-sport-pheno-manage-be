"""加载查询规划实体名称解析与相似候选检索的声明式配置。"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EntityMatchMode = Literal["name_fuzzy", "exact_only"]


class EntitySimilarityConfig(BaseModel):
    """限定一次名称相似候选计算的算法与资源预算。"""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["damerau_levenshtein"]
    threshold: float = Field(ge=0, le=1)
    max_edit_distance: int = Field(ge=0, le=10)
    max_candidates: int = Field(ge=1, le=10)
    max_scan_rows: int = Field(ge=1, le=10000)


class EntityLookupConfig(BaseModel):
    """定义一种可被用户语言指代的业务实体及其安全匹配字段。"""

    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    id_field: str = Field(min_length=1)
    display_field: str = Field(min_length=1)
    match_mode: EntityMatchMode
    similarity: EntitySimilarityConfig | None = None

    # 保证模糊匹配实体具备完整阈值和扫描预算，精确实体不会意外使用名称相似度。
    @model_validator(mode="after")
    def validate_similarity_configuration(self) -> "EntityLookupConfig":
        if (self.match_mode == "name_fuzzy") != (self.similarity is not None):
            raise ValueError("name_fuzzy 必须且只能配置 similarity")
        return self


class EntityLookupConfiguration(BaseModel):
    """承载全部实体匹配规则，并拒绝同表被映射为多个实体的歧义配置。"""

    model_config = ConfigDict(extra="forbid")

    entities: list[EntityLookupConfig] = Field(min_length=1)

    # 相似度字段选择必须唯一，避免检索工具依据配置顺序静默选择不同业务实体。
    @model_validator(mode="after")
    def validate_unique_table_mapping(self) -> "EntityLookupConfiguration":
        table_names = [entity.table_name for entity in self.entities]
        if len(table_names) != len(set(table_names)):
            raise ValueError("实体匹配配置中的 table_name 不能重复")
        return self


# 读取并缓存指定业务包的版本化配置，同时拒绝实体配置越过业务域表白名单。
@lru_cache(maxsize=16)
def load_entity_lookup_configuration(
    config_path: Path,
    allowed_tables: tuple[str, ...],
) -> EntityLookupConfiguration:
    try:
        raw_configuration = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"实体匹配配置不可用：{config_path}") from error
    configuration = EntityLookupConfiguration.model_validate(raw_configuration)
    forbidden_tables = sorted(
        {
            entity.table_name
            for entity in configuration.entities
            if entity.table_name not in allowed_tables
        }
    )
    if forbidden_tables:
        raise ValueError(
            "实体匹配配置包含业务域未授权表：" + "、".join(forbidden_tables)
        )
    return configuration


# 依据目标表获取唯一的实体匹配配置；非可命名实体返回 None，禁止对其字段执行模糊匹配。
def find_entity_lookup_config(
    configuration: EntityLookupConfiguration,
    table_name: str,
) -> EntityLookupConfig | None:
    for entity in configuration.entities:
        if entity.table_name == table_name:
            return entity
    return None
