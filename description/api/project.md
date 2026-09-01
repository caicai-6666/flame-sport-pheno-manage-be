# 运动项目管理 API

> **文档目的**
>
> 本文档定义管理端获取与创建运动项目、修改项目名称与可见状态，以及按项目和挑战等级读取规则内容的接口契约、筛选口径和数据边界。

## 1. 功能目标

项目路由集中承载管理端运动项目查询与基础状态管理能力：

- `list` 接口获取全部项目及其可见状态，由管理前端过滤隐藏项目；
- `rule` 接口使用项目 ID 与等级 ID 定位对应规则，并返回副描述、指标内容和规则备注；
- `create` 接口一次提交项目基础信息、全部等级规则、凭证上传配置和 WebP 图标；
- 项目名称接口在统一配置时间窗口内修改项目展示名称；
- 项目状态接口在统一配置时间窗口内切换可见或隐藏状态。

项目名称和状态修改都不会物理删除项目，也不会修改其说明、图标或关联规则。

---

## 2. `GET /project/list` 获取全部项目

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project/list` |
| Nginx 开发路径 | `/dev/flame/admin/api/project/list` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/project/list
Authorization: Bearer <admin-token>
```

### 2.2 查询口径

接口读取 `project` 表中的全部项目，不根据 `status`、当前赛季、项目规则或上传配置进行筛选。响应同步返回 `status`，管理前端按以下口径决定展示：

| `status` | 含义 | 前端处理 |
| --- | --- | --- |
| `1` | 可见 | 正常展示 |
| `0` | 隐藏 | 从普通可选项目中滤除，管理场景仍可识别 |

> **边界说明**
>
> 项目列表返回全部项目，不改变赛季创建接口的容量校验。新增赛季的 `required_project_count` 仍只与 `project.status = 1` 的当前可见项目数量比较。

结果按照 `project.id ASC` 排序，确保没有显式排序字段时仍具有稳定顺序。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "project_id": 1,
    "project_name": "跑步/快走",
    "description": "累计跑步里程，持续提升心肺能力",
    "icon_url": "/running.png",
    "status": 1
  },
  {
    "project_id": 2,
    "project_name": "健身打卡",
    "description": null,
    "icon_url": null,
    "status": 0
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | `integer` | 项目唯一标识 `project.id` |
| `project_name` | `string` | 项目展示名称 `project.name` |
| `description` | `string \| null` | 项目说明 `project.description`；未配置时为 `null` |
| `icon_url` | `string \| null` | 项目图标地址；未配置时为 `null` |
| `status` | `integer` | 项目可见状态；`1` 表示可见，`0` 表示隐藏 |

项目表没有任何记录时返回空数组 `[]`，状态码仍为 `200 OK`。仅存在隐藏项目时仍会返回这些项目，不会返回空数组。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不向前端暴露 SQL 或连接信息 |

---

## 3. `GET /project/rule` 获取项目等级规则

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project/rule` |
| Nginx 开发路径 | `/dev/flame/admin/api/project/rule` |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/project/rule?project_id=2&level_id=3
Authorization: Bearer <admin-token>
```

### 3.2 请求参数与查询口径

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `project_id` | `integer` | 是 | 大于 `0` | 运动项目 ID |
| `level_id` | `integer` | 是 | 大于 `0` | 挑战等级 ID |

管理前端需要展示或选择等级时，可以通过[挑战等级管理 API](project-level.md)获取 `level_id` 对应的名称与奖励积分。

项目与等级使用联合条件唯一定位规则：

```text
project_rule.project_id = project_id
project_rule.level_id = level_id
```

接口不根据当前赛季或规则状态进一步筛选。当前规则是平台通用规则，不关联赛季；保留停用规则的只读访问能力，可以支持历史参赛和凭证审核场景。

### 3.3 成功响应

状态码：`200 OK`。

```json
{
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
  "rule_note": "跑步或快走均可累计"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sub_desc` | `string \| null` | 挑战副描述 `project_rule.sub_desc`；未配置时为 `null` |
| `rule_content` | JSON | `project_rule.rule_content` 中保存的规则指标，保持数据库 JSON 结构返回 |
| `rule_note` | `string \| null` | 规则备注 `project_rule.rule_note`；未配置时为 `null` |

异步 MySQL 驱动当前会把 JSON 列读取为字符串，仓储负责将其解析成 JSON 值，前端不会收到二次序列化后的字符串。

新创建的挑战等级会为所有项目分别沿用各项目既有的指标名称，但指标 `value` 为 JSON `null`。因此前端读取新等级规则时必须兼容空值，在管理员补充目标值前不应把 `null` 渲染为可执行的挑战要求。

### 3.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| `project_id` 或 `level_id` 缺失、非整数或不大于 `0` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 对应规则不存在 | `404 Not Found` | 返回 `未找到对应的项目规则` |
| 数据库查询失败 | `500 Internal Server Error` | 不向前端暴露 SQL 或连接信息 |

---

## 4. `PATCH /project/{project_id}/status` 修改项目可见状态

### 4.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project/{project_id}/status` |
| Nginx 开发路径 | `/dev/flame/admin/api/project/{project_id}/status` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
PATCH /dev/flame/admin/api/project/2/status
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "status": 0
}
```

请求字段如下：

| 参数位置 | 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `project_id` | `integer` | 是 | 大于 `0` | 目标运动项目主键 |
| Body | `status` | `integer` | 是 | 只能为 `0` 或 `1` | `0` 表示隐藏，`1` 表示可见 |

`status` 采用严格整数校验，字符串 `"1"`、布尔值 `true` 及其他数字都会返回参数错误。请求体不接受额外字段。

### 4.2 修改规则

状态修改遵循以下业务规则：

- `status = 1` 时，项目进入客户端可见、可选项目口径；
- `status = 0` 时，项目从普通可选项目中隐藏，但管理端列表仍会返回该项目；
- 隐藏项目不会删除项目、项目规则、用户已选项目、凭证或历史统计关联；
- 重复提交与数据库当前值相同的状态仍返回成功，便于管理前端安全重试；
- 状态变化会影响后续赛季创建时可见项目数量的容量校验。

接口受 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 保护：

1. 没有 `status = 1` 的激活赛季时，允许预先调整项目状态。
2. 存在唯一激活赛季时，只允许在 `start_date 00:00` 起的配置窗口内修改。
3. 当前时间达到或超过截止时刻时，拒绝修改。
4. 同时存在多个激活赛季时，拒绝修改并报告赛季数据冲突。

窗口校验优先于项目查询。窗口已关闭时，即使项目不存在也先返回配置窗口冲突，不继续锁定项目记录。

### 4.3 成功响应

状态码：`200 OK`。接口返回修改后的完整项目基础信息。

```json
{
  "project_id": 2,
  "project_name": "健身打卡",
  "description": "记录每日训练",
  "icon_url": "/fitness.png",
  "status": 0
}
```

响应字段与 [`GET /project/list`](#2-get-projectlist-获取全部项目) 中的单个项目保持一致。

### 4.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| `project_id` 非正整数，`status` 缺失、类型错误或不属于 `0/1` | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| 运动项目不存在 | `404 Not Found` | `运动项目不存在` |
| 当前激活赛季的配置修改窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 数据库同时存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 5. `PATCH /project/{project_id}/name` 修改项目名称

### 5.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project/{project_id}/name` |
| Nginx 开发路径 | `/dev/flame/admin/api/project/{project_id}/name` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
PATCH /dev/flame/admin/api/project/2/name
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "name": "力量训练"
}
```

| 参数位置 | 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| Path | `project_id` | `integer` | 是 | 大于 `0` | 目标运动项目主键 |
| Body | `name` | `string` | 是 | 去除首尾空白后长度为 `1～64` | 新的项目展示名称 |

请求体不接受额外字段。项目名称受数据库唯一约束，不能与其他项目重名。

### 5.2 修改规则

名称修改受 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 保护，判断规则与项目可见状态修改一致：

1. 没有 `status = 1` 的激活赛季时允许修改。
2. 存在唯一激活赛季时，只允许在 `start_date 00:00` 起的配置窗口内修改。
3. 当前时间达到或超过截止时刻时拒绝修改。
4. 同时存在多个激活赛季时拒绝修改并报告赛季数据冲突。

服务在同一事务中先检查配置窗口，再锁定目标项目并修改名称。重复提交当前名称保持成功；名称变化不会修改项目主键，因此现有项目规则、上传配置、参赛项目和运动凭证仍关联同一个项目。

### 5.3 成功响应

状态码：`200 OK`。接口返回修改后的完整项目基础信息：

```json
{
  "project_id": 2,
  "project_name": "力量训练",
  "description": "记录每日训练",
  "icon_url": "/fitness.webp",
  "status": 1
}
```

### 5.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| `project_id` 非正整数，名称为空、超长或请求包含额外字段 | `422 Unprocessable Content` | FastAPI 请求校验错误 |
| 运动项目不存在 | `404 Not Found` | `运动项目不存在` |
| 新名称已经被其他项目使用 | `409 Conflict` | `运动项目名称已存在` |
| 当前激活赛季的配置修改窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 数据库同时存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 6. `POST /project/create` 创建运动项目

### 6.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/project/create` |
| Nginx 开发路径 | `/dev/flame/admin/api/project/create` |
| Content-Type | `multipart/form-data` |
| 认证要求 | 有效的管理员 Bearer Token |

请求包含三段 JSON 字符串和一个 WebP 文件：

| 表单字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project` | JSON 字符串 | 是 | 项目名称、说明和初始可见状态 |
| `project_rules` | JSON 字符串 | 是 | 新项目在当前全部挑战等级下的规则数组 |
| `project_upload_configs` | JSON 字符串 | 是 | 新项目支持的凭证类型和上传提示数组 |
| `icon_file` | File | 是 | 项目 WebP 图标 |

调用示例：

```http
POST /dev/flame/admin/api/project/create
Authorization: Bearer <admin-token>
Content-Type: multipart/form-data
```

`project` 示例：

```json
{
  "name": "骑行",
  "description": "通过骑行提升心肺耐力",
  "status": 0
}
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `name` | `string` | 去除首尾空白后长度为 `1`～`64` | 项目名称，数据库中必须唯一 |
| `description` | `string \| null` | 去除首尾空白后最长 `255` | 项目说明 |
| `status` | `integer` | 严格为 `0` 或 `1` | `0` 表示隐藏，`1` 表示可见 |

`project_rules` 示例：

```json
[
  {
    "level_id": 1,
    "sub_desc": "建立稳定骑行习惯",
    "rule_content": [
      {
        "label": "累计距离",
        "value": "100km"
      }
    ],
    "rule_note": null,
    "status": 1
  }
]
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `level_id` | `integer` | 大于 `0`，数组内不能重复 | 挑战等级主键 |
| `sub_desc` | `string \| null` | 最长 `128` | 该等级下的挑战副描述 |
| `rule_content` | `array` | `1`～`50` 项 | 项目评估指标 |
| `rule_content[].label` | `string` | 非空、最长 `255`，单条规则内唯一 | 指标名称 |
| `rule_content[].value` | JSON | 必填，可为 `null` | 该等级下的指标值 |
| `rule_note` | `string \| null` | 最长 `255` | 规则备注 |
| `status` | `integer` | 严格为 `0` 或 `1` | 规则启停状态 |

`project_upload_configs` 示例：

```json
[
  {
    "record_type": "普通凭证",
    "upload_hint": "上传骑行轨迹截图",
    "note_example": null,
    "sort_order": 0,
    "status": 1
  }
]
```

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `record_type` | `string` | 非空、最长 `64`，数组内唯一 | 凭证类型 |
| `upload_hint` | `string` | 非空、最长 `255` | 上传提示 |
| `note_example` | `string \| null` | 最长 `255` | 备注填写示例 |
| `sort_order` | `integer` | `0`～`4294967295` | 展示顺序 |
| `status` | `integer` | 严格为 `0` 或 `1` | 上传配置启停状态 |

每个 JSON 表单字段只接受接口声明的属性，不接受额外字段。`project_rules` 和 `project_upload_configs` 均至少包含一项、最多包含 `50` 项。

图标约束如下：

- 声明媒体类型必须为 `image/webp`；
- 实际文件格式必须是 WebP；
- 文件大小不能超过 `5 MiB`；
- 图片最长边不能超过 `1600` 像素。

### 6.2 创建规则与处理流程

创建项目属于高影响配置写入，受 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 保护。接口按照以下顺序执行：

1. 在进入事务前校验 WebP 媒体类型、真实格式、字节大小与像素尺寸。
2. 开启数据库事务并校验激活赛季配置窗口。
3. 共享锁定当前全部挑战等级，形成一致的等级快照。
4. 校验 `project_rules` **恰好覆盖全部等级**，不能遗漏、重复或包含未知等级。
5. 校验各等级 `rule_content` 使用完全相同、顺序一致的 `label` 列表；不同等级只改变对应 `value`。
6. 写入 `project`、`project_rule` 和 `project_upload_config`。
7. 生成 `/project-<随机标识>.webp` 唯一地址，并向客户端后端固定的 `POST /project_icon` 上传原始 WebP。
8. 上游确认保存成功后提交数据库事务。

> **图标地址规则**
>
> 每次创建都生成新的唯一 `icon_url`，不根据项目名称复用文件名，避免客户端后端和浏览器的历史缓存显示旧图标。

没有激活赛季时允许创建项目；存在唯一激活赛季时，仅能在配置窗口内创建；存在多个激活赛季或窗口已关闭时拒绝写入。

### 6.3 成功响应

状态码：`201 Created`。

```json
{
  "project_id": 8,
  "project_name": "骑行",
  "description": "通过骑行提升心肺耐力",
  "icon_url": "/project-97fc1a92e7704d0294cf0ca7f471c7cc.webp",
  "status": 0
}
```

响应字段与 [`GET /project/list`](#2-get-projectlist-获取全部项目) 中的单个项目保持一致。`icon_url` 是客户端后端已经确认保存的最终相对地址。

### 6.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 表单字段缺失、JSON 无效、字段类型或长度错误 | `422 Unprocessable Content` | FastAPI 校验错误或对应字段不是符合定义的 JSON 字符串 |
| `record_type` 重复 | `422 Unprocessable Content` | `project_upload_configs 不能包含重复 record_type` |
| 项目名称已存在 | `409 Conflict` | `运动项目名称已存在` |
| 规则未恰好覆盖全部挑战等级 | `409 Conflict` | `项目规则必须覆盖当前全部挑战等级` |
| 各等级指标标签或顺序不一致 | `409 Conflict` | `项目各等级的评估指标标签必须一致` |
| 图标声明或实际格式不是 WebP | `400 Bad Request` | 返回对应 WebP 格式提示 |
| 图标最长边超过 `1600` 像素 | `400 Bad Request` | `项目图标最长边不能超过 1600 像素` |
| 图标超过 `5 MiB` | `413 Content Too Large` | `项目图标不能超过 5 MiB` |
| 当前激活赛季配置窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 数据库存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 客户端后端拒绝图标路径或内容 | `400 Bad Request` | 转换为客户端后端提供的安全错误提示 |
| 客户端后端不可用或响应协议异常 | `502 Bad Gateway` | 返回项目图标上传服务不可用或响应异常提示，数据库事务回滚 |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 其他数据库写入失败 | `500 Internal Server Error` | 回滚事务，不暴露 SQL 或连接信息 |

---

## 7. 数据、事务与依赖

数据事实来源：

- [项目表说明](../db/project.md)；
- [挑战等级表说明](../db/project-level.md)；
- [项目规则表说明](../db/project-rule.md)；
- [项目上传配置表说明](../db/project-upload-config.md)。

代码入口：

- `app/router/project.py`
- `app/services/projects.py`
- `app/repositories/projects.py`
- `app/router/__init__.py`

列表和规则查询接口由应用服务在显式只读事务中调用仓储执行有界查询。项目创建会写入 `project`、`project_rule` 和 `project_upload_config`，并通过客户端后端保存图标；项目名称和状态修改接口都在单一写事务内按以下顺序执行：

1. 共享锁定激活赛季并校验配置时间窗口。
2. 使用 `FOR UPDATE` 锁定目标项目。
3. 参数化覆盖 `project.name` 或 `project.status`。
4. 提交事务并返回更新后的项目基础信息。

统一采用“赛季锁在前、项目锁在后”的顺序，避免不同高影响配置接口形成相反锁顺序。项目列表中的 `icon_url` 只作为字符串返回；读取图标文件时应调用 [图片安全中转 API](image.md) 的 `/image/project_icon` 接口。列表服务不替前端隐藏 `status = 0` 的项目。

项目创建为保证客户端后端上传失败时不留下已提交的项目记录，会在数据库事务内执行一次有超时限制的内部图标上传。若数据库最终提交失败，当前客户端后端没有删除图标接口，唯一地址对应的文件可能成为不可见孤立文件；它不会覆盖其他项目图标，后续应通过文件清理机制处理。

---

## 8. 验证方式

```bash
python -m unittest tests.test_project_list tests.test_project_rule tests.test_project_status tests.test_project_name tests.test_project_creation -v
```

测试覆盖全部项目、可空说明与状态映射、隐藏项目返回、稳定排序、空结果、联合规则查询、副描述与备注映射、JSON 还原、规则不存在，名称和状态修改的请求校验、管理员认证、配置窗口、项目行锁、事务、幂等写入和异常映射，以及项目创建的 multipart 解析、WebP 校验、规则矩阵、批量持久化、内部上传协议和失败回滚。

---

## 9. 已知限制

- 前端过滤隐藏项目时必须明确判断 `status = 1`，不能把接口返回即视为可选择。
- 当前没有项目自定义排序字段，因此按主键升序返回。
- `sub_desc` 和 `rule_note` 均允许为空，前端应在值为 `null` 时隐藏对应展示区域。
- 当前状态修改只控制全局可见口径；数据库没有赛季与可用项目关联，因此不能按单个赛季独立隐藏项目。
- 项目创建依赖客户端后端图标上传接口；客户端后端不可用时不会创建项目。
- 数据库最终提交失败后可能遗留使用唯一地址保存的孤立图标，当前没有跨服务原子事务或图标删除补偿接口。
