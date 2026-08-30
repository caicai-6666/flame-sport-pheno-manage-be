"""定义新原料协议下的结果塑形计划和列来源模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MaterialShapeColumn(BaseModel):
    """描述一个从 SQL 原料列透传到最终表格的可见字段。"""

    model_config = ConfigDict(extra="forbid")

    source_field: str = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="SQL 结果中提供该值的准确列名",
    )
    output_key: str = Field(
        min_length=1,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="最终表格使用的稳定 snake_case 字段键",
    )
    label: str = Field(
        min_length=1,
        max_length=50,
        description="最终表头使用的简洁中文业务名称，不附加用途解释",
    )


class MaterialResultShapePlan(BaseModel):
    """把自然语言塑形指导编译为程序可执行的透传或动态转列计划。"""

    model_config = ConfigDict(extra="forbid")

    shape_type: Literal["passthrough", "pivot"] = Field(
        description="passthrough 保持逐行结果，pivot 把同一主体的多行成员展开为动态列"
    )
    result_row_granularity: str = Field(
        min_length=1,
        description="塑形后每一行代表的业务主体",
    )
    passthrough_columns: list[MaterialShapeColumn] = Field(
        description="最终按顺序展示的普通字段；仅用于分组或排序的字段不得列入"
    )
    group_fields: list[str] = Field(
        description="pivot 使用的稳定主体键；passthrough 必须为空数组"
    )
    pivot_value_field: str | None = Field(
        default=None,
        description="pivot 依次展开的 SQL 结果列；passthrough 必须为 null",
    )
    pivot_order_field: str | None = Field(
        default=None,
        description="pivot 组内稳定排序使用的 SQL 结果列；无排序要求时为 null",
    )
    column_key_prefix: str | None = Field(
        default=None,
        description="pivot 动态列稳定键前缀，例如 sport_project；passthrough 必须为 null",
    )
    column_label_pattern: str | None = Field(
        default=None,
        description="pivot 动态列中文标题模板，必须且只能包含一次 {index}",
    )
    expected_pivot_columns: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="指导明确动态列数量时填写；否则为 null 并按完整结果最大组大小确定",
    )

    # 保证两种塑形模式只携带各自需要的字段，避免程序猜测模型遗漏的配置。
    @model_validator(mode="after")
    def validate_shape_mode(self) -> "MaterialResultShapePlan":
        output_keys = [item.output_key for item in self.passthrough_columns]
        if len(output_keys) != len(set(output_keys)):
            raise ValueError("passthrough_columns.output_key 不能重复")
        if len(self.group_fields) != len(set(self.group_fields)):
            raise ValueError("group_fields 不能重复")
        pivot_fields = (
            self.pivot_value_field,
            self.column_key_prefix,
            self.column_label_pattern,
        )
        if self.shape_type == "passthrough":
            if self.group_fields:
                raise ValueError("passthrough 的 group_fields 必须为空数组")
            if any(value is not None for value in pivot_fields) or (
                self.pivot_order_field is not None
                or self.expected_pivot_columns is not None
            ):
                raise ValueError("passthrough 不得携带 pivot 专属字段")
            return self
        if not self.group_fields:
            raise ValueError("pivot 的 group_fields 不能为空")
        if any(value is None for value in pivot_fields):
            raise ValueError(
                "pivot 必须提供 pivot_value_field、column_key_prefix 和 column_label_pattern"
            )
        assert self.column_key_prefix is not None
        assert self.column_label_pattern is not None
        if not self.column_key_prefix.isidentifier() or not self.column_key_prefix.islower():
            raise ValueError("column_key_prefix 必须是小写 snake_case")
        if self.column_label_pattern.count("{index}") != 1:
            raise ValueError("column_label_pattern 必须且只能包含一次 {index}")
        return self


__all__ = ["MaterialResultShapePlan", "MaterialShapeColumn"]
