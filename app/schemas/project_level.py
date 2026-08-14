"""定义挑战等级与项目规则配置接口的数据结构。"""

from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


class ProjectLevelResponse(BaseModel):
    id: int
    name: str
    reward: int


class CreateProjectLevelRequest(BaseModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
    ]
    reward: int = Field(ge=0, le=4_294_967_295)


class UpdateProjectLevelRewardRequest(BaseModel):
    reward: int = Field(ge=0, le=4_294_967_295)


class ProjectRuleMetricValueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    value: JsonValue


class UpdateProjectRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_content: list[ProjectRuleMetricValueRequest] | None = Field(
        default=None,
        min_length=1,
    )
    sub_desc: Annotated[str, StringConstraints(max_length=128)] | None = None
    rule_note: Annotated[str, StringConstraints(max_length=255)] | None = None

    # 拒绝空补丁、显式空指标列表及重复标签，避免产生无意义或含糊写入。
    @model_validator(mode="after")
    def validate_patch_fields(self) -> Self:
        submitted_fields = self.model_fields_set
        if not submitted_fields:
            raise ValueError("至少提交一个可修改字段")
        if "rule_content" in submitted_fields and self.rule_content is None:
            raise ValueError("rule_content 不能为 null")
        if self.rule_content is not None:
            labels = [metric.label for metric in self.rule_content]
            if len(labels) != len(set(labels)):
                raise ValueError("rule_content 不能包含重复 label")
            if any(not label.strip() for label in labels):
                raise ValueError("rule_content 的 label 不能为空")
        return self


class ProjectRuleConfigurationResponse(BaseModel):
    project_id: int
    level_id: int
    sub_desc: str | None
    rule_content: JsonValue
    rule_note: str | None
