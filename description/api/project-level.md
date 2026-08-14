# 挑战等级管理 API

> **文档目的**
>
> 本文档定义管理端查询、创建挑战等级、修改奖励积分及配置项目等级规则的接口契约、数据口径、事务规则和认证边界，为赛季配置提供稳定的管理能力。

## 1. 功能目标

`project-level` 路由独立承载挑战等级相关的管理端能力：

- `list` 接口返回等级主键、展示名称和赛季挑战成功后的奖励积分；
- `create` 接口根据名称和奖励积分创建默认启用的新等级，并为所有项目初始化对应规则；
- 奖励积分修改接口根据等级主键覆盖 `reward`，并返回修改后的完整等级信息；
- 项目规则修改接口根据项目与等级定位配置，只允许调整既有指标的 `value`、`sub_desc` 和 `rule_note`。

> **查询结论**
>
> “全部等级”包括启用和停用记录。接口不会依据 `project_level.status` 过滤，以便管理端识别历史赛季和历史规则引用的等级。

---

## 2. `GET /project-level/list` 获取全部等级

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project-level/list` |
| Nginx 开发路径 | `/dev/flame/admin/api/project-level/list` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/project-level/list
Authorization: Bearer <admin-token>
```

### 2.2 查询与排序口径

接口读取 `project_level` 表中的全部记录，不根据启停状态、当前赛季或项目规则进行筛选。

结果按照以下顺序返回：

```text
reward ASC, id ASC
```

奖励积分较低的等级优先；奖励相同时使用主键保证顺序稳定。接口不返回 `status`，因为本次契约只要求等级 ID、名称和积分值。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "id": 1,
    "name": "青铜",
    "reward": 100
  },
  {
    "id": 2,
    "name": "白银",
    "reward": 200
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 挑战等级主键 `project_level.id` |
| `name` | `string` | 挑战等级展示名称 `project_level.name` |
| `reward` | `integer` | 完成该等级挑战后对应的奖励积分 `project_level.reward` |

数据库没有等级记录时返回空数组 `[]`，状态码仍为 `200 OK`。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不返回虚假空数组，也不暴露 SQL 或连接信息 |

---

## 3. `POST /project-level/create` 创建等级

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project-level/create` |
| Nginx 开发路径 | `/dev/flame/admin/api/project-level/create` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```json
{
  "name": "铂金",
  "reward": 400
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | `string` | 是 | 去除首尾空白后长度为 `1～32` | 挑战等级名称，必须全局唯一 |
| `reward` | `integer` | 是 | `0～4294967295` | 完成该等级挑战后获得的奖励积分 |

### 3.2 创建规则

新等级固定使用以下状态：

```text
status = 1
```

创建等级时，后端还会为 `project` 表中的**所有项目**创建一条 `(project_id, level_id)` 规则，包括当前 `status = 0` 的隐藏项目。每个项目的新规则沿用该项目已有等级的有序评估指标名称，并把指标值初始化为 JSON `null`：

```json
[
  {
    "label": "累计距离",
    "value": null
  },
  {
    "label": "配速要求",
    "value": null
  }
]
```

初始化规则的其他默认值为：

```text
sub_desc = NULL
rule_note = NULL
status = 1
```

同一项目在已有不同等级下的 `label` 内容和顺序必须一致。任一项目没有已有指标模板、模板为空或跨等级指标不一致时，接口拒绝创建，避免新增等级缺少完整的项目规则矩阵。

等级名称受 `project_level.name` 唯一键保护。接口不采用“先查询、后写入”判断重名，而是在单一事务中直接写入并处理数据库重复键错误，从而避免两个并发请求同时通过预检查后创建同名等级。

`reward = 0` 是合法值，表示该等级挑战完成后不发放奖励积分。接口不会自动推导积分值，也不会修改已有等级。

创建接口受 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 保护。没有 `status = 1` 的激活赛季时允许预先创建；存在唯一激活赛季时，只允许在 `start_date` 当日 `00:00` 起的配置窗口内创建。达到截止时刻后拒绝写入。

### 3.3 成功响应

状态码：`201 Created`。

```json
{
  "id": 4,
  "name": "铂金",
  "reward": 400
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 数据库生成的新等级主键 |
| `name` | `string` | 去除首尾空白后实际写入的等级名称 |
| `reward` | `integer` | 实际写入的奖励积分 |

### 3.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 名称为空、超过 `32` 字符，或请求字段缺失 | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| `reward` 不是整数、小于 `0` 或超过 `4294967295` | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| 挑战等级名称已经存在 | `409 Conflict` | `挑战等级名称已存在` |
| 至少一个项目没有已有评估指标模板 | `409 Conflict` | `存在未配置评估指标的项目，无法创建挑战等级` |
| 同一项目在已有等级下的评估指标不一致 | `409 Conflict` | `项目评估指标配置不一致，无法创建挑战等级` |
| 当前激活赛季的配置修改窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 数据库同时存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 非重名的数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 4. `PATCH /project-level/{level_id}/reward` 修改奖励积分

### 4.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project-level/{level_id}/reward` |
| Nginx 开发路径 | `/dev/flame/admin/api/project-level/{level_id}/reward` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
PATCH /dev/flame/admin/api/project-level/2/reward
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "reward": 260
}
```

| 参数位置 | 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `level_id` | `integer` | 是 | 大于 `0` | 要修改的挑战等级主键 |
| Body | `reward` | `integer` | 是 | `0～4294967295` | 赛季挑战完成后的新奖励积分 |

### 4.2 修改规则

接口只覆盖目标 `project_level.reward`，不会修改等级名称、启停状态或项目规则。`reward = 0` 合法；重复提交与数据库当前值相同的积分仍返回成功，便于前端安全重试。

写入前执行统一赛季配置窗口校验：

1. 没有 `status = 1` 的激活赛季时，允许修改。
2. 唯一激活赛季尚未达到 `start_date 00:00 + ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 时，允许修改。
3. 当前时间达到或超过截止时刻时，拒绝修改。
4. 同时存在多个激活赛季时，因无法确定唯一窗口而拒绝修改。

窗口校验优先于目标等级查询。窗口已经关闭或赛季数据冲突时，接口直接返回 `409 Conflict`，不会继续锁定或修改等级记录。

### 4.3 成功响应

状态码：`200 OK`。

```json
{
  "id": 2,
  "name": "白银",
  "reward": 260
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 实际修改的挑战等级主键 |
| `name` | `string` | 等级当前名称 |
| `reward` | `integer` | 更新后实际保存的奖励积分 |

### 4.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| `level_id` 不是正整数 | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| `reward` 缺失、不是整数或超出字段范围 | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| 挑战等级不存在 | `404 Not Found` | `挑战等级不存在` |
| 当前激活赛季的配置修改窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 数据库同时存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 5. `PATCH /project-level/{level_id}/project/{project_id}/rule` 修改项目规则

### 5.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project-level/{level_id}/project/{project_id}/rule` |
| Nginx 开发路径 | `/dev/flame/admin/api/project-level/{level_id}/project/{project_id}/rule` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
PATCH /dev/flame/admin/api/project-level/3/project/2/rule
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "rule_content": [
    {
      "label": "累计距离",
      "value": "50km"
    }
  ],
  "sub_desc": "提升有氧容量和节奏控制",
  "rule_note": null
}
```

路径参数如下：

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `level_id` | `integer` | 是 | 大于 `0` | 目标挑战等级主键 |
| `project_id` | `integer` | 是 | 大于 `0` | 目标运动项目主键 |

请求体字段如下。三个字段均可选，但一次请求必须至少提交一个字段：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `rule_content` | `array` | 非空；`label` 不得重复 | 按既有 `label` 局部更新对应的 `value` |
| `rule_content[].label` | `string` | 长度 `1～255`，不能仅含空白 | 必须与数据库当前规则中的标签完全一致 |
| `rule_content[].value` | JSON | 必填，可以为 `null` | 指标的新值；允许使用字符串、数字、布尔值、数组、对象或 `null` |
| `sub_desc` | `string \| null` | 字符串最长 `128` 字符 | 新副描述；传 `null` 表示清空，未提交表示保持原值 |
| `rule_note` | `string \| null` | 字符串最长 `255` 字符 | 新规则备注；传 `null` 表示清空，未提交表示保持原值 |

额外字段会被拒绝，不能通过请求体提交新的 `label`、修改标签顺序或覆盖规则状态。

### 5.2 修改规则

`rule_content` 采用**按标签局部更新**语义。后端锁定现有 `(project_id, level_id)` 规则后，对请求中的每个 `label` 查找既有指标，只替换该指标的 `value`：

- 未提交的指标保持原值；
- 所有既有指标的 `label` 和顺序保持不变；
- 指标对象中除 `value` 外的其他扩展字段保持不变；
- 请求包含未知标签或重复标签时拒绝整个请求；
- 数据库既有 `rule_content` 不是标签唯一的 JSON 数组时拒绝修改，避免静默破坏历史配置。

接口受 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 保护，判断口径与创建等级、修改奖励积分一致：

1. 没有 `status = 1` 的激活赛季时，允许预先配置。
2. 存在唯一激活赛季时，只允许在 `start_date 00:00` 起的配置窗口内修改。
3. 当前时间达到或超过截止时刻时，拒绝修改。
4. 同时存在多个激活赛季时，拒绝修改并报告赛季数据冲突。

窗口校验优先于规则查询。接口重复提交与当前配置相同的值时仍返回成功，便于管理前端安全重试。

### 5.3 成功响应

状态码：`200 OK`。接口返回更新后的完整配置，而不是仅返回本次局部补丁。

```json
{
  "project_id": 2,
  "level_id": 3,
  "sub_desc": "提升有氧容量和节奏控制",
  "rule_content": [
    {
      "label": "累计距离",
      "value": "50km"
    },
    {
      "label": "配速要求",
      "value": "≤8'00''"
    }
  ],
  "rule_note": null
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | `integer` | 实际修改的运动项目主键 |
| `level_id` | `integer` | 实际修改的挑战等级主键 |
| `sub_desc` | `string \| null` | 更新后的挑战副描述 |
| `rule_content` | JSON | 更新后的完整规则指标数组 |
| `rule_note` | `string \| null` | 更新后的规则备注 |

### 5.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 路径 ID 非正整数，请求为空，字段越界或指标标签重复 | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| `(project_id, level_id)` 没有对应项目规则 | `404 Not Found` | `未找到对应的项目规则` |
| 请求包含既有规则中不存在的指标标签 | `409 Conflict` | `规则指标标签与现有配置不一致` |
| 既有 `rule_content` 不是可安全修改的标准结构 | `409 Conflict` | `现有项目规则指标格式异常，无法修改` |
| 当前激活赛季的配置修改窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 数据库同时存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 6. 数据、事务与安全

列表接口由应用服务开启显式只读事务，再调用等级仓储完成单次有界查询。创建接口在同一写事务中执行以下流程：

1. 共享锁定激活赛季并校验配置窗口。
2. 共享锁定全部项目和现有项目规则，取得一致的指标模板快照。
3. 校验每个项目都存在模板，且同一项目跨等级指标一致。
4. 插入默认启用的新挑战等级。
5. 批量插入该等级对应的全部项目规则。
6. 全部成功后提交；任一步失败都回滚等级和规则写入。

积分修改接口在同一事务中先共享锁定激活赛季并校验窗口，再使用 `FOR UPDATE` 锁定目标等级并覆盖积分。统一采用“赛季锁在前、业务记录锁在后”的顺序，降低多个配置写接口并发时发生死锁的风险。

项目规则修改接口也使用单一写事务：先共享锁定激活赛季并校验窗口，再使用 `FOR UPDATE` 锁定 `(project_id, level_id)` 规则，校验既有指标结构与请求标签，最后一次覆盖完整的 `sub_desc`、`rule_content` 和 `rule_note`。行锁保证两个并发补丁不会基于同一旧版本互相覆盖。

所有接口均不会访问客户端后端服务。批量插入避免按项目逐条往返数据库；项目与等级联合唯一键继续作为规则矩阵的最终一致性约束。

相关位置：

- [项目等级表说明](../db/project-level.md)
- [项目表说明](../db/project.md)
- [项目规则表说明](../db/project-rule.md)
- [运动项目管理 API](project.md)
- `app/router/project_level.py`
- `app/services/project_levels.py`
- `app/repositories/project_levels.py`
- `app/repositories/projects.py`
- `app/services/configuration_guard.py`
- `app/repositories/configuration_guard.py`
- `app/router/__init__.py`

所有请求继承统一管理员认证依赖。错误响应不得泄露数据库连接信息、SQL 内容或认证凭据。

---

## 7. 验证方式

```bash
python -m unittest tests.test_configuration_guard tests.test_project_level tests.test_project_rule_update -v
```

测试覆盖：

- 全部等级字段映射和稳定排序；
- 查询不按 `status` 过滤；
- 空数据返回空数组；
- 服务层只读事务；
- 创建请求字段校验和默认启用写入；
- 全部项目的指标标签继承和 JSON 空值初始化；
- 隐藏项目同样生成规则；
- 缺失模板与跨等级指标不一致冲突；
- 创建事务提交及异常回滚；
- 并发同名所依赖的数据库唯一键冲突映射；
- 非重复键完整性错误保持原始失败语义；
- 激活赛季的配置窗口、截止边界和多激活赛季冲突；
- 积分修改的等级行锁、字段覆盖、重复请求与不存在分支；
- 积分修改的请求校验、错误映射和认证；
- 项目规则修改的配置窗口守卫、联合标识行锁和事务回滚；
- 指标值局部更新时保留标签、顺序、未提交指标和扩展字段；
- `sub_desc`、`rule_note` 的修改、保留和显式清空语义；
- 未知标签、重复标签、非法历史 JSON 结构和规则不存在分支；
- 项目规则修改的请求边界、完整响应、异常映射和认证；
- HTTP 响应序列化；
- 统一管理员认证。

---

## 8. 已知限制

- 当前接口不返回等级启停状态，不能用于直接判断等级是否仍允许新用户选择。
- 当前没有分页；等级属于规模有限的基础配置，因此一次返回全部记录。
- 当前支持修改等级奖励积分和项目等级规则内容，但不支持修改等级名称或等级、规则的启停状态。
- 规则指标标签属于项目模板结构，只能通过本接口修改其值；当前没有修改、增删或重排标签的管理接口。
- 创建接口没有业务幂等键；重复提交相同名称会稳定返回 `409 Conflict`，不会创建第二条记录。
- 当项目已经存在但系统尚无任何项目规则模板时，无法通过本接口创建首个等级；需要先建立可继承的项目指标模板。
