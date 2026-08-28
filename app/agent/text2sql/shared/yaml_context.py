"""统一 Text-to-SQL 模型上下文的可读 YAML 渲染与受控解析。"""

from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel
import yaml


class _ReadableYamlDumper(yaml.SafeDumper):
    """为模型上下文保留中文、字段顺序和多行文本结构的安全 YAML Dumper。"""


# 将包含换行的文本表示为 YAML 块标量，使 SQL、表结构和长说明保持原始视觉层次。
def _represent_readable_string(
    dumper: _ReadableYamlDumper,
    value: str,
) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ReadableYamlDumper.add_representer(str, _represent_readable_string)


# 将 Pydantic、枚举、日期和 Decimal 递归转换为 SafeDumper 能稳定处理的基础值。
def _to_yaml_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _to_yaml_safe(value.model_dump())
    if isinstance(value, Enum):
        return _to_yaml_safe(value.value)
    if isinstance(value, dict):
        return {str(key): _to_yaml_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_yaml_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# 以固定字段顺序和块状列表渲染模型动态事实，避免 JSON 引号和转义干扰阅读。
def render_yaml_context(value: Any) -> str:
    return yaml.dump(
        _to_yaml_safe(value),
        Dumper=_ReadableYamlDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    ).strip()


# 只解析系统自身生成的安全 YAML，并要求调用方继续按具体业务模型校验结构。
def parse_yaml_context(content: str) -> Any:
    return yaml.safe_load(content)


# 将以空行分隔、使用大写标签的业务配置记录转成统一小写键 YAML 列表。
def parse_tagged_context_records(content: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current_record: dict[str, Any] = {}
    active_list_key: str | None = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            if current_record:
                records.append(current_record)
                current_record = {}
                active_list_key = None
            continue
        if line.startswith("- "):
            if active_list_key is None:
                raise ValueError("业务上下文列表项缺少所属字段")
            list_value = current_record[active_list_key]
            if not isinstance(list_value, list):
                raise ValueError(f"业务上下文字段 {active_list_key} 不是列表")
            list_value.append(line[2:].strip())
            continue
        if ":" not in line:
            raise ValueError(f"业务上下文行缺少键值分隔符：{line}")
        raw_key, raw_value = line.split(":", maxsplit=1)
        key = raw_key.strip().lower()
        value = raw_value.strip()
        if value:
            current_record[key] = value
            active_list_key = None
        else:
            current_record[key] = []
            active_list_key = key
    if current_record:
        records.append(current_record)
    return records


# 将旧标签配置转换为带稳定根键的合法 YAML，资源文件无需同时维护第二份数据。
def render_tagged_context_as_yaml(content: str, root_key: str) -> str:
    return render_yaml_context(
        {root_key: parse_tagged_context_records(content)}
    )
