# 赛季结算 API

> **文档目的**
>
> 本文档定义管理端结算路由的查询、终审、积分发放和一键赛季收口接口。

## 1. 功能目标与通用规则

结算子路由用于查询和处理 `status = 2` 的唯一结算中赛季。FastAPI 前缀为 `/flame/admin/api/settlement`，Nginx 开发前缀为 `/dev/flame/admin/api/settlement`。

通用规则：

- 所有接口均要求有效的管理员 Bearer Token；
- 查询只认数据库中的 `season.status = 2`，不根据日期推导；
- 查询接口不推进赛季状态，也不触发初审、终审、定分或积分发放；
- 同时存在多个结算中赛季属于数据一致性冲突。

---

## 2. `GET /settlement/current` 获取当前结算赛季

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/settlement/current` |
| Nginx 开发路径 | `/dev/flame/admin/api/settlement/current` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

### 2.2 查询口径

接口返回唯一 `status = 2` 的赛季。`season_user_ids` 只包含正式参赛记录，必须同时满足：

```text
season_user.season_id = season.id
season_user.level_id IS NOT NULL
season_user.status >= season.required_project_count
```

列表包含已定分和未定分用户，不按 `final_points` 或 `points_issued` 过滤，并按 `season_user.id` 升序返回。

### 2.3 成功响应

状态码：`200 OK`。

```json
{
  "season_id": 6,
  "name": "2026年7月赛季",
  "start_date": "2026-07-01",
  "end_date": "2026-07-31",
  "required_project_count": 3,
  "status": 2,
  "season_user_ids": [78, 79, 80]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `season_id` | `integer` | 结算赛季主键 |
| `name` | `string` | 赛季名称 |
| `start_date` | `date` | 赛季开始日期 |
| `end_date` | `date` | 赛季结束日期 |
| `required_project_count` | `integer` | 正式参赛要求的项目数量 |
| `status` | `integer` | 固定为 `2` |
| `season_user_ids` | `integer[]` | 正式参赛记录主键列表；无人时为空数组 |

### 2.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 当前没有结算中赛季 | `404 Not Found` | `当前没有结算中的赛季` |
| 同时存在多个结算中赛季 | `409 Conflict` | `存在多个结算中赛季，无法确定当前结算赛季` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不暴露 SQL 和连接信息 |

---

## 3. `POST /settlement/participants` 批量获取结算用户详情

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/settlement/participants` |
| Nginx 开发路径 | `/dev/flame/admin/api/settlement/participants` |
| 认证要求 | 有效的管理员 Bearer Token |

请求体：

```json
{
  "season_user_ids": [80, 78, 84]
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `season_user_ids` | `integer[]` | 是 | `1～1000` 项，每项大于 `0` | 待查询的赛季参赛记录主键 |

重复 ID 只查询并返回一次，响应位置按首次出现顺序排列。不属于当前结算赛季、不满足正式参赛条件或不存在的 ID 直接省略。

### 3.2 查询口径

每条返回记录必须属于当前唯一 `status = 2` 的赛季，并满足正式参赛条件。查询关联：

- `user`：用户 ID、用户名和头像地址；
- `department`：部门名称；
- `project_level`：挑战等级名称；
- 有效 `season_user_project` 与 `project`：运动项目和当前完成进度；
- `season_user`：最终积分和积分发放状态。

项目按 `season_user_project.id` 升序排列。历史项目或等级即使当前已停用，只要仍被有效参赛记录引用，仍按其名称展示。

### 3.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "season_user_id": 80,
    "user_id": "<user-id>",
    "username": "李四",
    "department_name": "产品部",
    "avatar_url": "/avatar/user-80.webp",
    "level_name": "白银挑战",
    "projects": [
      {
        "project_id": 1,
        "project_name": "日常步数",
        "completion_progress": 1.0
      },
      {
        "project_id": 2,
        "project_name": "跑步/快走",
        "completion_progress": 0.75
      }
    ],
    "final_points": 20,
    "points_issued": false
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `season_user_id` | `integer` | 赛季参赛记录主键 |
| `user_id` | `string` | 用户主键 |
| `username` | `string` | 用户展示名称 |
| `department_name` | `string` | 用户所属部门名称 |
| `avatar_url` | `string \| null` | 用户头像地址 |
| `level_name` | `string` | 用户锁定的挑战等级名称 |
| `projects` | `array` | 用户本赛季的有效运动项目进度 |
| `projects[].project_id` | `integer` | 运动项目主键 |
| `projects[].project_name` | `string` | 运动项目名称 |
| `projects[].completion_progress` | `number` | 当前完成进度，范围为 `0～1` |
| `final_points` | `integer \| null` | 本赛季待发放积分；`null` 表示尚未定分 |
| `points_issued` | `boolean` | `final_points` 是否已经计入全局积分流水 |

全部 ID 均被省略时返回空数组 `[]` 和 `200 OK`。

### 3.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 请求体缺失、列表为空、ID 非正整数或超过 1000 项 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 当前没有结算中赛季 | `404 Not Found` | `当前没有结算中的赛季` |
| 同时存在多个结算中赛季 | `409 Conflict` | `存在多个结算中赛季，无法确定当前结算赛季` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不暴露 SQL 和连接信息 |

---

## 4. `GET /settlement/pending-final-reviews` 获取全部待终审凭证

### 4.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/settlement/pending-final-reviews` |
| Nginx 开发路径 | `/dev/flame/admin/api/settlement/pending-final-reviews` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

### 4.2 查询口径

接口先确认数据库中只有一个 `status = 2` 的结算赛季，再以一次联表查询返回该赛季全部正式参赛用户的待终审凭证。记录必须同时满足：

```text
season_user.level_id IS NOT NULL
season_user.status >= season.required_project_count
proof_record.review_status = preliminary_approved
proof_record.status = 1
```

`pending`、`preliminary_rejected`、`approved`、`rejected` 和已作废凭证均不返回。结果依次按照 `proof_date`、`created_at` 和 `id` 倒序排列，使最近记录优先展示。

### 4.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "proof_record_id": 501,
    "season_user_id": 80,
    "project_id": 2,
    "image_url": "/proofs/501.jpg",
    "created_at": "2026-07-30T20:15:00",
    "proof_date": "2026-07-30",
    "note": "晚间跑步 5 公里",
    "preliminary_review_comment": "初审符合单次要求",
    "review_comment": null
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `proof_record_id` | `integer` | 待终审凭证主键 `proof_record.id`，可直接传给终审接口 |
| `season_user_id` | `integer` | 正式参赛记录主键，可关联结算用户详情 |
| `project_id` | `integer` | 凭证所属项目主键 |
| `image_url` | `string` | 凭证图片地址，仅用于数据兼容 |
| `created_at` | `datetime` | 凭证实际上传时间 |
| `proof_date` | `date` | 凭证对应运动日期 |
| `note` | `string \| null` | 用户运动备注 |
| `preliminary_review_comment` | `string \| null` | 大模型初审意见 |
| `review_comment` | `string \| null` | 管理员终审意见；待终审记录通常为 `null` |

没有符合条件的凭证时返回空数组 `[]` 和 `200 OK`。

### 4.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 当前没有结算中赛季 | `404 Not Found` | `当前没有结算中的赛季` |
| 同时存在多个结算中赛季 | `409 Conflict` | `存在多个结算中赛季，无法确定当前结算赛季` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不暴露 SQL 和连接信息 |

---

## 5. `POST /settlement/final-review` 记录结算终审

### 5.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/settlement/final-review` |
| Nginx 开发路径 | `/dev/flame/admin/api/settlement/final-review` |
| 认证要求 | 有效的管理员 Bearer Token |

请求体：

```json
{
  "proof_record_id": 501,
  "review_comment": "凭证符合要求",
  "decision": "approved"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `proof_record_id` | `integer` | 是 | 大于 `0` | 待终审队列返回的凭证主键 |
| `review_comment` | `string \| null` | 是 | 非空时为 `1～500` 个字符 | 终审评语；无需评语时传 `null` |
| `decision` | `string` | 是 | `approved` 或 `rejected` | 终审决定 |

### 5.2 处理规则

接口复用[凭证终审 API](proof.md)的状态校验、通过、拒绝、进度回扣与回补逻辑，但强制凭证属于当前结算中赛季的正式参赛用户。普通赛季凭证不能通过该入口终审。

决定为 `approved` 时，在同一事务内：

1. 将凭证更新为终审通过并把请求中的 `review_comment` 保存为终审意见；保留 `preliminary_review_comment`，初审已经计入的 `increase` 和项目进度保持不变。
2. 将该凭证对应的 `season_supplement_eligibility.status` 更新为 `0`，关闭继续补传资格。
3. 重新检查该用户是否仍有 `preliminary_approved` 凭证或 `status = 1` 的补传资格。
4. 阻塞条件全部消失后，按最终项目完成数、挑战等级和连续完成月份自动计算积分。
5. 写入 `season_user.final_points` 和赛季结算通知，保持 `points_issued = 0`，等待积分发放接口处理。

决定为 `rejected` 时，复用普通终审的进度回扣与候选凭证回补逻辑。对应补传资格保持有效，使用户后续仍可补传；若业务规则判定该凭证已经不影响项目达成或用户属于零完成禁补分支，则按结算规则自动收口。

终审、进度调整、资格更新、自动定分和通知写入共享一个数据库事务，任一步失败都会整体回滚。

### 5.3 成功响应

状态码：`200 OK`。响应沿用普通终审结构：

```json
{
  "proof_record_id": 501,
  "review_status": "approved",
  "review_comment": "凭证符合要求",
  "rolled_back_progress": 0.0,
  "backfilled_progress": 0.0,
  "completion_progress": null
}
```

`final_points` 不在本响应中重复返回；管理端可通过结算用户详情接口刷新该用户的定分状态。

### 5.4 异常处理

| 场景 | 状态码 | `detail` |
| --- | --- | --- |
| 请求字段缺失、ID 非正整数、评语为空白或决定值非法 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 凭证不存在或已失效 | `404 Not Found` | `凭证不存在或已失效` |
| 凭证不属于当前结算赛季 | `409 Conflict` | `凭证不属于当前结算赛季` |
| 凭证已终审或当前状态不允许终审 | `409 Conflict` | `凭证已完成终审或当前状态不允许终审` |
| 凭证贡献与项目进度不一致 | `409 Conflict` | `凭证贡献与项目进度不一致，无法完成终审` |

---

## 6. `POST /settlement/issue-points` 发放赛季积分

### 6.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/settlement/issue-points` |
| Nginx 开发路径 | `/dev/flame/admin/api/settlement/issue-points` |
| 认证要求 | 有效的管理员 Bearer Token |

请求体：

```json
{
  "season_user_id": 78
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `season_user_id` | `integer` | 是 | 大于 `0` | 待发放积分的正式赛季参赛记录主键 |

### 6.2 发放规则

首次发放必须同时满足：

```text
season_user.final_points IS NOT NULL
season_user.points_issued = 0
season.status = 2
```

系统按以下顺序在一个事务中处理：

1. 读取目标所属用户并锁定 `user` 主记录，串行化同一用户的积分余额变化。
2. 锁定正式 `season_user`，重新校验定分、发放状态和赛季状态。
3. 锁定并读取该用户最新有效 `point_record`；不存在流水时当前余额按 `0` 计算。
4. 新增一条 `change_type = season_reward`、`product_id = NULL` 的有效积分流水。
5. 将 `season_user.points_issued` 从 `0` 条件更新为 `1`。

流水字段规则：

```text
change_points = season_user.final_points
points_after = 最新有效流水 points_after + final_points
gift_distribution_status = pending
```

未完成全部项目但至少完成一项时，明确展示保底积分：

```text
恭喜您在{赛季名称}达成{完成项目数}/{赛季项目数}个项目目标，获得20分保底积分！感谢您的坚持，下个赛季继续向全部达成冲刺！
```

全部项目完成且没有连续完成奖励时，展示挑战等级积分：

```text
恭喜您达成{赛季名称}{挑战等级名称}，获得{挑战积分}分挑战积分
```

全部项目完成且存在连续完成奖励时，分别展示挑战积分、额外奖励积分和合计：

```text
恭喜您达成{赛季名称}{挑战等级名称}，获得{挑战积分}分挑战积分、{额外奖励}分连续完成额外奖励积分，合计{最终积分}分
```

`final_points = 0` 时仍写入零积分 `season_reward` 流水并完成发放标记，提示为：

```text
{赛季名称}{挑战等级名称}结算完成：本赛季暂未达成项目目标，本次积分为0分。感谢您的参与，坚持运动就是进步，下个赛季继续加油！
```

系统根据最终完成项目数、挑战等级积分和 `final_points` 复核积分构成。保底积分不是 `20` 分，或完整完成后的 `final_points` 小于挑战等级积分时，视为定分数据不一致并回滚发放。完整完成用户的连续奖励由 `final_points - 挑战等级积分` 还原，因此环境配置调整后仍可发放调整前已经持久化的结算结果。

积分发放不再创建钉钉通知；定分阶段生成的 `赛季结算结果` 通知继续承担结算结果告知，积分流水 `description` 承担本次发放提示。

### 6.3 幂等与并发

目标已经 `points_issued = 1` 时直接返回成功，`issued_now = false`，不会读取余额或新增流水。该幂等结果允许在赛季随后进入已结束状态后继续返回。

用户主记录锁保证同一用户的其他积分写入不能并发覆盖 `points_after`；`season_user` 行锁与 `points_issued = 0` 条件更新共同防止同一赛季奖励重复发放。流水插入和状态更新任一步失败都会回滚。

全部正式参赛用户完成定分和积分发放，并且不存在有效补传资格后，定时任务才能把赛季从结算中更新为已结束。

### 6.4 成功响应

状态码：`200 OK`。

首次发放：

```json
{
  "season_user_id": 78,
  "final_points": 100,
  "points_issued": true,
  "issued_now": true
}
```

重复请求的 `issued_now` 为 `false`，其他字段保持相同。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `season_user_id` | `integer` | 已处理的正式参赛记录主键 |
| `final_points` | `integer` | 本次赛季定分结果 |
| `points_issued` | `boolean` | 成功响应固定为 `true` |
| `issued_now` | `boolean` | `true` 表示本次新增流水，`false` 表示此前已经发放 |

### 6.5 异常处理

| 场景 | 状态码 | `detail` |
| --- | --- | --- |
| ID 缺失、非整数或不大于 `0` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 记录不存在或不属于正式参赛用户 | `404 Not Found` | `正式赛季参赛记录不存在` |
| `final_points = NULL` | `409 Conflict` | `该用户尚未完成赛季定分` |
| 未发放记录所属赛季不处于结算中 | `409 Conflict` | `该赛季当前不允许发放积分` |
| 发放后余额超过 `INT UNSIGNED` 范围 | `409 Conflict` | `发放后的用户积分余额超出允许范围` |
| 用户归属、条件更新或积分数据不一致 | `409 Conflict` | `积分数据不一致，无法完成发放` |

---

## 7. `POST /settlement/complete` 一键完成赛季结算

### 7.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/settlement/complete` |
| Nginx 开发路径 | `/dev/flame/admin/api/settlement/complete` |
| 请求参数 | 无 |
| 请求体 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

接口只处理数据库中唯一的 `status = 2` 赛季。该操作代表管理员放弃继续等待终审或补传，并立即以当前终审结果收口整个赛季。

### 7.2 处理规则

全部处理位于一个数据库事务中，并严格按以下顺序执行：

1. 读取唯一结算中赛季及其全部 `season_user`，按稳定顺序锁定关联用户、赛季和参与记录。
2. 将该赛季全部有效 `pending` 和 `preliminary_approved` 凭证统一更新为 `rejected`，覆盖固定结算意见并把 `increase` 归零。
3. 每个有效项目只按 `approved` 凭证的 `progress_delta` 重新分配进度，单项目总进度封顶为 `1.0000`。
4. 为被自动拒绝凭证的用户写入一条汇总通知，并在结束赛季前清空整张 `season_supplement_eligibility` 表。
5. 对正式参赛且 `final_points IS NULL` 的用户，根据重算后的当前进度写入最终积分和结算结果通知；已经定分的用户不重新计算。
6. 对所有正式参赛且 `points_issued = 0` 的用户写入 `season_reward` 流水，包括最终积分为 `0` 的用户，并将发放状态更新为已发放。
7. 确认所有正式参赛用户均已定分、已发放且不存在开放补传资格后，将赛季从 `2` 更新为 `3`。

非正式参与用户的未审核凭证也会关闭，但不会生成赛季积分。补传资格表在所有用户处理完成后整表清空，不保留已经关闭的历史资格。`preliminary_rejected`、`approved`、`rejected` 和无效凭证不会被改写；其中 `approved` 凭证是最终进度的唯一贡献来源。

若已经持久化的 `final_points` 与重算后的进度档位不一致，或者项目数量、用户归属、积分余额等数据违反既有约束，接口会回滚全部操作，不会只结束部分用户。

### 7.3 成功响应

状态码：`200 OK`。

```json
{
  "season_id": 6,
  "participant_count": 20,
  "rejected_proof_count": 8,
  "finalized_user_count": 5,
  "issued_user_count": 12,
  "season_ended": true
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `season_id` | `integer` | 本次完成结算的赛季主键 |
| `participant_count` | `integer` | 正式参赛用户总数 |
| `rejected_proof_count` | `integer` | 本次自动拒绝的待初审和待终审凭证数 |
| `finalized_user_count` | `integer` | 本次新写入 `final_points` 的用户数 |
| `issued_user_count` | `integer` | 本次新写入积分流水并完成发放的用户数 |
| `season_ended` | `boolean` | 成功响应固定为 `true` |

### 7.4 事务、并发与重复请求

接口先按用户 ID 稳定加锁，再锁定结算赛季和全部参与记录，并在写入前复核赛季与人员集合没有变化。并发终审、积分发放或另一个一键结算请求不能造成重复流水；检测到锁定期间状态变化时，本次请求整体回滚并返回冲突。

成功后赛季已经变为 `status = 3`，重复请求返回当前没有结算中赛季，不会重复拒绝凭证、定分或发放积分。若调用方因网络超时无法确定结果，应重新查询结算赛季和目标赛季状态，不能假定请求失败后立即重复执行其他人工写入。

### 7.5 异常处理

| 场景 | 状态码 | `detail` |
| --- | --- | --- |
| 当前没有结算中赛季 | `404 Not Found` | `当前没有结算中的赛季` |
| 同时存在多个结算中赛季 | `409 Conflict` | `存在多个结算中赛季，无法确定当前结算赛季` |
| 锁定期间赛季或参与记录发生变化 | `409 Conflict` | `赛季结算数据不一致，无法完成一键结算` |
| 项目进度、定分、用户归属或积分数据不一致 | `409 Conflict` | `赛季结算数据不一致，无法完成一键结算` |
| 发放后的用户积分余额超过无符号整数范围 | `409 Conflict` | `发放后的用户积分余额超出允许范围` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |

---

## 8. 数据、事务与安全边界

数据事实来源：

- [赛季表](../db/season.md)
- [赛季用户表](../db/season-user.md)
- [赛季用户项目表](../db/season-user-project.md)
- [用户表](../db/user.md)
- [部门表](../db/department.md)
- [挑战等级表](../db/project-level.md)
- [项目表](../db/project.md)
- [积分变动记录表](../db/point-record.md)

代码入口：

- `app/router/settlement.py`
- `app/schemas/settlement.py`
- `app/services/season_settlements.py`
- `app/repositories/season_settlements.py`

查询应用服务在一个只读事务中确认唯一结算赛季并读取目标记录，避免两次查询观察到不同状态。详情仓储使用一次参数化批量联表查询并在内存中按请求顺序聚合项目；待终审仓储也使用一次联表查询读取整个赛季队列，均不会按用户或项目产生 N+1 查询。

结算终审复用普通终审核心，并额外强制结算赛季归属；终审状态、进度调整、补传资格和可能发生的定分处于同一事务。单用户积分发放使用单一写事务，按“用户主记录、赛季参赛记录、最新有效积分流水”的顺序加锁，再插入积分流水和更新发放标记。

一键结算把凭证拒绝、项目进度重算、补传资格表清空、未定分用户定分、积分流水发放和赛季结束放入同一事务。它会锁定整个赛季涉及的用户和参与记录，既供管理员立即收口，也供达到环境配置期限的后台任务复用。路由继承统一管理员认证，错误响应不包含 SQL、用户余额明细或内部锁信息。

---

## 9. 验证方式

```bash
python -m unittest tests.test_settlement_route -v
```

测试覆盖正式参赛口径、ID 去重、用户和部门映射、项目进度聚合、全赛季待终审筛选与序列化、结算专用终审、普通赛季隔离、资格关闭、自动定分触发、定分与发放状态、稳定排序、参数边界、空赛季、多结算赛季冲突、首次发放、零积分流水、最新余额累计、重复幂等、条件更新冲突、余额上限、一键拒绝与进度重算、原子赛季结束、事务和统一认证。

---

## 10. 已知限制

- 当前一次返回结算赛季的全部正式参赛记录主键，尚未分页；后续数据量需要分页时应新增明确的游标契约。
- 当前一次返回结算赛季的全部待终审凭证，尚未分页；数据量增长后应为该队列增加游标分页。
- 批量详情接口单次最多接受 1000 个 `season_user_id`；更大列表应由调用方分批请求。
- 当前接口不返回补传资格或积分流水明细。
- `point_record` 当前没有 `season_user_id` 字段，赛季奖励来源由 `season_user.points_issued`、流水类型和描述共同表达；如后续需要按赛季流水做强外键审计，应另行评估表结构。
- 一键结算为了保证全有或全无会持有赛季范围内的用户、参与、项目和凭证锁；赛季数据量显著增长后，应在压测基础上评估维护窗口、超时和持久化批次方案，不能直接拆成会暴露部分提交的普通分页事务。
