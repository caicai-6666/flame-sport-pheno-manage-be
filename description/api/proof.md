# 凭证终审 API

> **文档目的**
>
> 本文档定义管理端按赛季用户记录查询待终审凭证的接口契约、审核状态口径、返回字段和安全边界。

## 1. 功能目标

管理员在赛季期间需要持续终审已经通过初审的运动凭证。`proof` 路由提供待终审队列查询和终审结果写入；终审拒绝会在同一事务中撤销并重新分配项目进度。

---

## 2. `GET /proof/pending-final-review` 查询待终审凭证

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/proof/pending-final-review` |
| Nginx 开发路径 | `/dev/flame/admin/api/proof/pending-final-review` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
GET /dev/flame/admin/api/proof/pending-final-review?season_user_id=101
Authorization: Bearer <admin-token>
```

### 2.2 请求参数与查询口径

| 参数 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `season_user_id` | `integer` | 是 | 大于 `0` | 待查询的赛季用户记录主键 `season_user.id` |

接口只返回同时满足以下条件的凭证：

```text
proof_record.season_user_id = 请求 season_user_id
proof_record.review_status = preliminary_approved
proof_record.status = 1
```

`approved`、`rejected`、`pending`、`preliminary_rejected` 和已作废记录均不返回。结果依次按照 `proof_date`、`created_at` 和 `id` 倒序排列，使最近需要处理的记录优先展示。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "id": 501,
    "project_id": 5,
    "image_url": "/proofs/501.jpg",
    "created_at": "2026-08-12T10:30:45",
    "proof_date": "2026-08-11",
    "note": "晚间跑步 5 公里",
    "review_comment": "距离满足单次要求"
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 凭证记录主键 `proof_record.id` |
| `project_id` | `integer` | 凭证所属项目 |
| `image_url` | `string` | 凭证图片地址，仅用于数据兼容 |
| `created_at` | `datetime` | 实际上传时间，使用 ISO 8601 格式 |
| `proof_date` | `date` | 实际运动日期，格式为 `YYYY-MM-DD` |
| `note` | `string \| null` | 用户运动备注 |
| `review_comment` | `string \| null` | 初审意见 |

没有匹配记录时返回空数组 `[]` 和 `200 OK`。空数组不区分参赛记录不存在、全部已终审或全部被其他条件排除。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| `season_user_id` 缺失、非整数或不大于 `0` | `422 Unprocessable Entity` | FastAPI 参数校验错误，且不查询数据库 |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不转换成虚假空结果，也不暴露 SQL 和连接信息 |

---

## 3. `POST /proof/final-review` 记录终审结果

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/proof/final-review` |
| Nginx 开发路径 | `/dev/flame/admin/api/proof/final-review` |
| 认证要求 | 有效的管理员 Bearer Token |

请求示例：

```http
POST /dev/flame/admin/api/proof/final-review
Authorization: Bearer <admin-token>
Content-Type: application/json
```

### 3.2 请求体与处理规则

```json
{
  "proof_record_id": 501,
  "review_comment": "凭证缺少有效日期信息",
  "decision": "rejected"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `proof_record_id` | `integer` | 是 | 大于 `0` | 待终审凭证 ID |
| `review_comment` | `string \| null` | 是 | 非空时去除首尾空白后为 `1～500` 个字符 | 终审评语，覆盖初审意见；无需评语时传 `null` |
| `decision` | `string` | 是 | `approved` 或 `rejected` | 终审决定 |

只有 `status = 1` 且 `review_status = preliminary_approved` 的凭证可以终审。已经终审或处于其他初审状态的凭证不能重复提交，避免重复撤销进度。

决定为 `approved` 时，接口将 `review_status` 更新为 `approved` 并覆盖 `review_comment`，不改变凭证 `increase` 或项目进度。

决定为 `rejected` 时，在同一事务中执行：

1. 锁定凭证所属的有效 `season_user_project` 进度行。
2. 重新锁定目标凭证并确认仍然待终审。
3. 将目标凭证的 `review_status` 改为 `rejected`、覆盖评语，并将 `increase` 归零。
4. 从项目进度中撤销目标凭证原有的 `increase`。
5. 遍历同一 `season_user_id + project_id` 下仍有效、审核通过且 `progress_delta > increase` 的其他凭证。
6. 优先使用 `approved` 凭证回补，再使用 `preliminary_approved` 凭证；同一优先级按 `created_at ASC, id ASC` 分配。
7. 写回候选凭证的 `increase` 和最终 `completion_progress`。
8. 创建标题为 `运动凭证终审结果` 的 `pending` 通知，保存审核结果、运动项目、凭证日期和审核意见。

终审通过不创建通知。终审拒绝未填写审核意见时，通知中使用 `未填写`。

> **进度口径**
>
> 回退量使用被拒凭证的 `increase`，而不是 `progress_delta`；单条候选最多补入 `progress_delta - increase`。候选不足时，项目进度保留实际下降结果。

### 3.3 成功响应

状态码：`200 OK`。

终审拒绝示例：

```json
{
  "proof_record_id": 501,
  "review_status": "rejected",
  "review_comment": "凭证缺少有效日期信息",
  "rolled_back_progress": 0.4,
  "backfilled_progress": 0.25,
  "completion_progress": 0.75
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `proof_record_id` | `integer` | 已终审凭证 ID |
| `review_status` | `string` | 最终状态 `approved` 或 `rejected` |
| `review_comment` | `string \| null` | 已保存的终审评语；未填写时为 `null` |
| `rolled_back_progress` | `number` | 本次从目标凭证撤销的实际贡献；通过时为 `0` |
| `backfilled_progress` | `number` | 其他凭证本次实际回补总量；通过时为 `0` |
| `completion_progress` | `number \| null` | 拒绝并完成回补后的项目进度；通过时为 `null`，表示未调整 |

### 3.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 请求字段缺失、ID 非正整数、评语为空白或决定值非法 | `422 Unprocessable Entity` | FastAPI 参数校验错误；无评语应显式传 `null` |
| 凭证不存在或 `status != 1` | `404 Not Found` | `凭证不存在或已失效` |
| 凭证已经终审或不处于初审通过状态 | `409 Conflict` | `凭证已完成终审或当前状态不允许终审` |
| 有效用户项目不存在，或凭证贡献大于当前项目进度 | `409 Conflict` | `凭证贡献与项目进度不一致，无法完成终审` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库写入失败 | `500 Internal Server Error` | 整个事务回滚，不暴露 SQL 和连接信息 |

---

## 4. 数据、事务与安全边界

数据事实来源：

- [凭证记录表说明](../db/proof-record.md)
- [赛季用户表说明](../db/season-user.md)
- [赛季用户项目表说明](../db/season-user-project.md)
- [用户通知表说明](../db/notification.md)

代码入口：

- `app/router/proof.py`
- `app/services/proofs.py`
- `app/repositories/proofs.py`

待终审查询在显式只读事务中执行。终审拒绝把状态更新、进度撤销、候选回补、项目进度写回和通知创建放在同一个事务中；项目行锁用于串行化同一用户项目下的进度分配。通知写入失败时终审事务整体回滚。管理前端读取图片时应把凭证 `id` 传给 [图片安全中转 API](image.md) 的 `/image/proof_record/{proof_record_id}`。

---

## 5. 验证方式

```bash
python -m unittest tests.test_proof tests.test_proof_final_review -v
```

测试覆盖待终审查询，以及终审通过不通知、拒绝通知、拒绝回退、终审通过凭证优先回补、回补不足、重复终审、进度一致性、通知失败回滚、参数校验和认证。

---

## 6. 已知限制

- 当前按一个 `season_user_id` 返回全部待终审记录，尚未分页。
- 终审接口尚未记录管理员个人身份和独立操作审计，因为当前认证体系只有共享管理员密钥。
- 凭证图片通过独立的安全中转接口读取，不在列表中内嵌图片内容。
