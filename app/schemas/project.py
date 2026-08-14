"""定义运动项目管理接口及 multipart JSON 字段的数据结构。"""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    model_validator,
)


class ProjectInformationResponse(BaseModel):
    project_id: int
    project_name: str
    description: str | None
    icon_url: str | None
    status: Literal[0, 1]


class ProjectRuleResponse(BaseModel):
    sub_desc: str | None
    rule_content: JsonValue
    rule_note: str | None


class UpdateProjectVisibilityStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int = Field(strict=True, ge=0, le=1)


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=255),
    ] | None = None
    status: int = Field(strict=True, ge=0, le=1)


class CreateProjectRuleMetricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]
    value: JsonValue


class CreateProjectRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level_id: int = Field(strict=True, gt=0)
    sub_desc: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=128),
    ] | None = None
    rule_content: list[CreateProjectRuleMetricRequest] = Field(
        min_length=1,
        max_length=50,
    )
    rule_note: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=255),
    ] | None = None
    status: int = Field(strict=True, ge=0, le=1)

    # 拒绝单条规则内的重复标签，确保指标可以被稳定定位和后续局部修改。
    @model_validator(mode="after")
    def validate_unique_metric_labels(self) -> Self:
        labels = [metric.label for metric in self.rule_content]
        if len(labels) != len(set(labels)):
            raise ValueError("rule_content 不能包含重复 label")
        return self


class CreateProjectUploadConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    upload_hint: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]
    note_example: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=255),
    ] | None = None
    sort_order: int = Field(strict=True, ge=0, le=4_294_967_295)
    status: int = Field(strict=True, ge=0, le=1)


PROJECT_FORM_ADAPTER = TypeAdapter(CreateProjectRequest)
PROJECT_RULES_FORM_ADAPTER = TypeAdapter(
    Annotated[
        list[CreateProjectRuleRequest],
        Field(min_length=1, max_length=50),
    ]
)
PROJECT_UPLOAD_CONFIGS_FORM_ADAPTER = TypeAdapter(
    Annotated[
        list[CreateProjectUploadConfigurationRequest],
        Field(min_length=1, max_length=50),
    ]
)
