# 积分商城商品 API

> **文档目的**
>
> 本文档定义管理端新增奖品、查询完整商品目录、修改商品基础资料与可见状态、查询待发放兑换礼品、读取奖品基础信息，以及处理礼品发放或拒绝审核的接口契约、数据口径、状态变化和异常处理。

## 1. 功能目标

`product` 路由承载积分商城商品新增、查询、资料维护、上下架管理与履约相关的管理端能力。当前接口可以新增带 WebP 图片的奖品、读取全部商品及其完整字段，局部修改商品图片、名称、兑换积分和描述，切换商品可见状态，查询有效的待发放兑换流水、按奖品 ID 查询展示信息，并对指定兑换流水确认发放或拒绝发放。

确认发放只修改履约状态；拒绝发放会修改原流水状态和提示，并通过新增退款流水补回积分。两种操作都不会改写原兑换流水的 `change_points`、`points_after` 或有效状态。

---

## 2. `GET /product/pending-distributions` 查询待发放礼品

### 2.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/pending-distributions` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/pending-distributions` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/product/pending-distributions
Authorization: Bearer <admin-token>
```

### 2.2 查询口径

接口只返回同时满足以下条件的积分流水：

```text
point_record.change_type = 'exchange'
point_record.gift_distribution_status = 'pending'
point_record.status = 1
point_record.product_id IS NOT NULL
```

前两个条件是待发放兑换的核心口径；后两个条件用于排除已作废流水和缺少商品关联的异常数据。

结果按照以下顺序升序排列：

1. `point_record.created_at`，优先处理较早兑换的礼品；
2. `point_record.id`，在创建时间相同时提供稳定顺序。

### 2.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "id": 31,
    "user_id": "user-1",
    "product_id": 5,
    "description": "兑换商品：运动水杯",
    "created_at": "2026-08-12T09:30:00"
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 积分兑换流水主键 `point_record.id` |
| `user_id` | `string` | 兑换礼品的用户 ID |
| `product_id` | `integer` | 待发放的商品 ID |
| `description` | `string \| null` | 兑换流水描述，未填写时为 `null` |
| `created_at` | `datetime` | 兑换时间，取自 `point_record.created_at`，使用 ISO 8601 格式 |

没有待发放礼品时返回空数组 `[]`，状态码仍为 `200 OK`。

### 2.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不返回虚假空数组，也不暴露 SQL 或连接信息 |

---

## 3. `GET /product/info` 获取奖品信息

### 3.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/info` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/info` |
| 请求参数 | Query 参数 `product_id` |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/product/info?product_id=5
Authorization: Bearer <admin-token>
```

请求参数：

| 字段 | 类型 | 是否必填 | 规则与说明 |
| --- | --- | --- | --- |
| `product_id` | `integer` | 是 | 奖品主键 `product.id`，必须大于 `0` |

管理端需要读取历史兑换关联的奖品，因此接口不按 `product.status` 过滤。已经下架但仍保留数据库记录的奖品可以正常查询。

### 3.2 成功响应

状态码：`200 OK`。

```json
{
  "name": "运动水杯",
  "description": "运动补水",
  "image_url": "/products/bottle.png"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | 奖品名称 |
| `description` | `string \| null` | 奖品描述，未配置时为 `null` |
| `image_url` | `string \| null` | 奖品图片地址，未配置时为 `null`；有值时可传给[商品图片中转接口](image.md)读取图片 |

### 3.3 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 缺少 `product_id` 或值不大于 `0` | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 奖品记录不存在 | `404 Not Found` | `奖品不存在` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不暴露 SQL 或连接信息 |

---

## 4. `GET /product/list` 获取全部商品列表

### 4.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/list` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/list` |
| 请求参数 | 无 |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
GET /dev/flame/admin/api/product/list
Authorization: Bearer <admin-token>
```

### 4.2 查询口径

接口返回 `product` 表中的全部字段，不按 `status` 过滤，因此上架和下架商品都会出现在结果中。管理前端可以使用响应中的 `status` 执行展示筛选，历史商品不会因下架而失去管理可见性。

结果按照 `product.id ASC` 排序，保证没有业务排序字段时仍具有稳定顺序。本接口不关联积分流水，也不读取商品图片二进制；需要显示图片时，应把 `image_url` 传给[商品图片中转接口](image.md)。

### 4.3 成功响应

状态码：`200 OK`。

```json
[
  {
    "id": 1,
    "name": "运动水杯",
    "description": "运动补水",
    "points_required": 50,
    "image_url": "/运动水杯.jpg",
    "status": 1
  },
  {
    "id": 2,
    "name": "旧款跳绳",
    "description": null,
    "points_required": 30,
    "image_url": null,
    "status": 0
  }
]
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 商品主键 `product.id` |
| `name` | `string` | 商品名称 |
| `description` | `string \| null` | 商品说明，未配置时为 `null` |
| `points_required` | `integer` | 兑换商品所需积分 |
| `image_url` | `string \| null` | 商品图片相对地址，未配置时为 `null` |
| `status` | `integer` | 商品状态；`1` 表示上架，`0` 表示下架 |

商品表没有记录时返回空数组 `[]`，状态码仍为 `200 OK`。

### 4.4 异常处理

| 场景 | 状态码 | 处理方式 |
| --- | --- | --- |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库查询失败 | `500 Internal Server Error` | 不返回虚假空数组，也不暴露 SQL 或连接信息 |

---

## 5. `PATCH /product/{product_id}/status` 修改商品可见状态

### 5.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/{product_id}/status` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/{product_id}/status` |
| Content-Type | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
PATCH /dev/flame/admin/api/product/2/status
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "status": 0
}
```

### 5.2 请求参数与状态规则

| 参数位置 | 字段 | 类型 | 必填 | 约束与说明 |
| --- | --- | --- | --- | --- |
| Path | `product_id` | `integer` | 是 | 商品主键，必须大于 `0` |
| Body | `status` | `integer` | 是 | 严格为 `0` 或 `1`；`0` 表示下架，`1` 表示上架 |

请求体不接受额外字段。字符串 `"1"`、布尔值 `true`、负数和其他整数都不会隐式转换为有效状态。

状态修改遵循以下规则：

- 下架只修改 `product.status`，不会物理删除商品，也不会修改商品名称、描述、积分价格或图片地址。
- 历史兑换流水继续保留商品关联，下架商品仍可通过管理端商品列表和详情接口读取。
- 重复提交与数据库当前值相同的状态仍返回成功，便于管理前端安全重试。
- 服务在写事务内通过 `FOR UPDATE` 锁定目标商品，防止并发管理操作基于不一致的商品状态执行。

> **配置窗口边界**
>
> 当前 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 已明确约束奖品积分值修改，但没有约束商品上下架。本接口不执行赛季配置窗口校验；若产品后续要求统一限制，应在确认业务口径后调整。

### 5.3 成功响应

状态码：`200 OK`。接口返回更新后的全部商品字段。

```json
{
  "id": 2,
  "name": "运动水杯",
  "description": "运动补水",
  "points_required": 50,
  "image_url": "/运动水杯.jpg",
  "status": 0
}
```

响应字段定义与 [`GET /product/list`](#4-get-productlist-获取全部商品列表) 的单个商品一致。

### 5.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| `product_id` 不是正整数 | `422 Unprocessable Content` | FastAPI 路径参数校验错误 |
| `status` 缺失、类型错误、不属于 `0/1` 或请求体含额外字段 | `422 Unprocessable Content` | FastAPI 请求体校验错误 |
| 商品不存在 | `404 Not Found` | `奖品不存在` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库读取或写入失败 | `500 Internal Server Error` | 当前事务回滚，不暴露 SQL 或连接信息 |

---

## 6. `PATCH /product/{product_id}` 修改奖品基本信息

### 6.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/{product_id}` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/{product_id}` |
| Content-Type | `multipart/form-data` |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```bash
curl -X PATCH 'http://<管理端地址>/dev/flame/admin/api/product/7' \
  -H 'Authorization: Bearer <admin-token>' \
  -F 'product={"name":"Keep 弹力带","points_required":80,"description":"居家力量训练"}' \
  -F 'image=@./keep-band.webp;type=image/webp'
```

### 6.2 请求字段与局部更新规则

| 参数位置 | 字段 | 类型 | 必填 | 约束与说明 |
| --- | --- | --- | --- | --- |
| Path | `product_id` | `integer` | 是 | 商品主键，必须大于 `0` |
| Form | `product` | `string` | 否 | JSON 字符串，结构见下表，最长 `4096` 个字符 |
| File | `image` | `file` | 否 | WebP 图片；声明媒体类型必须为 `image/webp`，真实格式必须为 WebP，最大 `5 MiB` |

`product` JSON 字段定义：

| 字段 | 类型 | 必填 | 约束与说明 |
| --- | --- | --- | --- |
| `name` | `string` | 否 | 奖品名称；去除首尾空白后为 `1～128` 个字符，不能传 `null` |
| `points_required` | `integer` | 否 | 兑换积分；严格为 `0～4294967295` 的整数，不接受字符串或布尔值 |
| `description` | `string \| null` | 否 | 奖品描述，最多 `255` 个字符；显式传 `null` 表示清空描述 |

`product` 内全部字段均可选，`image` 也可选，但整个请求必须至少提交一个商品字段或一张图片。未提交字段保持数据库原值，`product` 不接受未定义字段。只更新文字或积分时可以不上传 `image`；只替换图片时可以不传 `product`。

管理端不接受调用方提供 `image_url`。收到有效 WebP 后，服务会生成形如 `/product-<32位随机值>.webp` 的唯一相对地址，避免替换后仍命中旧图片缓存。仅修改文件名或请求媒体类型不能绕过真实图片格式校验。

只有实际提交 `points_required` 字段时，接口才执行 `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 校验：

- 没有激活赛季时允许修改。
- 存在唯一激活赛季时，只允许在赛季开始后的配置窗口内修改。
- 窗口关闭或存在多个激活赛季时返回 `409 Conflict`。
- 只修改名称、描述或图片时不受该配置窗口限制。

### 6.3 图片上传与替换顺序

上传图片时，处理顺序固定为：

1. 限量读取上传内容，并校验大小、媒体类型和真实 WebP 格式。
2. 生成不复用的 `.webp` 相对地址。
3. 在数据库事务内锁定目标商品，更新所有已提交字段及新图片地址。
4. 提交数据库事务。
5. 查询旧图片是否仍被其他商品引用。
6. 最后以 `multipart/form-data` 调用客户端后端 `POST /flame/api/admin/product/replace`，上传原始 WebP 字节并按需清理旧文件。

管理端向客户端后端发送的字段为：

```text
old_image_url=/旧奖品.jpg
new_image_url=/product-9ee5f1ccf70d4c7ea6711fcb8956461d.webp
image=<WebP 二进制>
```

旧图片仍被其他商品引用时，管理端会省略 `old_image_url`，使客户端只写入新图片而不删除共享旧文件。管理端还会校验客户端响应中的 `image_url` 和 `size_bytes` 是否与请求一致，拒绝接受协议异常响应。

> **重要的部分成功语义**
>
> 按照“图片替换必须最后执行”的业务要求，客户端调用发生在数据库提交之后。客户端不可用或拒绝上传时，数据库修改**不会回滚**；接口返回 `502 Bad Gateway` 并明确说明商品基本信息已更新。此时数据库可能已经指向尚未成功写入的图片地址，前端应重新拉取商品列表并提示人工重试，不得把旧表单值直接当作服务端最终状态。

### 6.4 成功响应

状态码：`200 OK`。接口返回更新后的全部商品字段。

```json
{
  "id": 7,
  "name": "Keep 弹力带",
  "description": "居家力量训练",
  "points_required": 80,
  "image_url": "/product-9ee5f1ccf70d4c7ea6711fcb8956461d.webp",
  "status": 1
}
```

响应字段定义与 [`GET /product/list`](#4-get-productlist-获取全部商品列表) 的单个商品一致。

### 6.5 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| `product_id` 不是正整数 | `422 Unprocessable Content` | FastAPI 路径参数校验错误 |
| 没有提交任何商品字段或图片 | `422 Unprocessable Content` | `至少提交一个可修改字段或奖品图片` |
| `product` 不是合法 JSON、字段为非法 `null`、类型或长度错误、含额外字段 | `422 Unprocessable Content` | `product 必须是符合接口定义的 JSON 字符串` 或 FastAPI 表单校验错误 |
| 图片声明媒体类型不是 `image/webp` | `400 Bad Request` | `奖品图片只支持 WebP 格式` |
| 图片为空、无法解码或真实格式不是 WebP | `400 Bad Request` | `上传内容不是有效的奖品图片` |
| 图片超过 `5 MiB` | `413 Content Too Large` | `奖品图片不能超过 5 MiB` |
| 商品不存在 | `404 Not Found` | `奖品不存在`；数据库未写入，也不调用客户端 |
| 提交积分字段但配置窗口已关闭 | `409 Conflict` | `当前激活赛季的配置修改窗口已关闭` |
| 提交积分字段但存在多个激活赛季 | `409 Conflict` | `存在多个激活赛季，无法判断配置修改窗口` |
| 数据库读取或写入失败 | `500 Internal Server Error` | 数据库事务回滚，不调用客户端，也不暴露 SQL 或连接信息 |
| 数据库已提交，但客户端图片替换返回错误、响应异常、连接失败或超时 | `502 Bad Gateway` | `奖品基本信息已更新，但图片替换失败：<安全提示>` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |

---

## 7. `POST /product/create` 新增奖品

### 7.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/create` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/create` |
| Content-Type | `multipart/form-data` |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```bash
curl -X POST 'http://<管理端地址>/dev/flame/admin/api/product/create' \
  -H 'Authorization: Bearer <admin-token>' \
  -F 'name=运动毛巾' \
  -F 'points_required=80' \
  -F 'description=训练后快速吸汗' \
  -F 'image=@./towel.webp;type=image/webp'
```

### 7.2 请求字段与创建规则

| 表单字段 | 类型 | 必填 | 约束与说明 |
| --- | --- | --- | --- |
| `name` | `string` | 是 | 去除首尾空白后长度为 `1～128` 个字符 |
| `points_required` | `integer` | 是 | `0～4294967295`，不接受负数、布尔值或非整数字符串 |
| `description` | `string` | 否 | 最长 `255` 个字符；缺省、空字符串或纯空白统一保存为 `null` |
| `image` | `file` | 是 | 声明媒体类型和真实格式都必须是 WebP，最大 `5 MiB` |

创建规则如下：

- 新奖品默认保存为 `status = 1`，即创建成功后直接上架。
- 商品名称不要求唯一，商品始终通过数据库生成的 `id` 唯一识别。
- 管理端校验图片后生成 `/product-<32位随机值>.webp` 唯一地址，调用方不能自行指定 `image_url`。
- `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 当前只约束既有奖品积分值修改，**新增奖品不执行该配置窗口校验**。

### 7.3 数据库与图片落盘顺序

处理顺序固定为：

1. 限量读取并校验图片媒体类型、真实 WebP 格式和 `5 MiB` 大小上限。
2. 生成不复用的 `.webp` 相对地址。
3. 在数据库事务中写入完整商品记录并提交。
4. 最后复用客户端后端 `POST /flame/api/admin/product/replace`，传入空旧地址、新地址和 WebP 文件完成落盘。

> **重要的部分成功语义**
>
> 图片存储必须位于数据库操作之后。客户端后端不可用或拒绝图片时，已提交的商品记录不会回滚；接口返回 `502 Bad Gateway`，此时商品记录可能指向尚未成功落盘的图片地址。管理端应重新加载列表并提示人工重试。

### 7.4 成功响应

状态码：`201 Created`。

```json
{
  "id": 12,
  "name": "运动毛巾",
  "description": "训练后快速吸汗",
  "points_required": 80,
  "image_url": "/product-6fd4f630049c4a7c8a4ad07054a2db1e.webp",
  "status": 1
}
```

响应字段定义与 [`GET /product/list`](#4-get-productlist-获取全部商品列表) 的单个商品一致。

### 7.5 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 必填字段缺失，名称、积分或描述不符合约束 | `422 Unprocessable Content` | FastAPI 表单校验错误或 `奖品名称、兑换积分或描述不符合字段约束` |
| 图片声明媒体类型不是 `image/webp` | `400 Bad Request` | `奖品图片只支持 WebP 格式` |
| 图片为空、无法解码或真实格式不是 WebP | `400 Bad Request` | `上传内容不是有效的奖品图片` |
| 图片超过 `5 MiB` | `413 Content Too Large` | `奖品图片不能超过 5 MiB` |
| 数据库写入失败 | `500 Internal Server Error` | 数据库事务回滚，不调用客户端，也不暴露 SQL 或连接信息 |
| 数据库已提交，但客户端图片存储失败 | `502 Bad Gateway` | `奖品已创建，但图片存储失败：<安全提示>` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |

---

## 8. `POST /product/distribute` 处理礼品发放审核

### 8.1 接口定义

| 项目 | 内容 |
| --- | --- |
| FastAPI 路径 | `/flame/admin/api/product/distribute` |
| Nginx 开发路径 | `/dev/flame/admin/api/product/distribute` |
| 请求类型 | `application/json` |
| 认证要求 | 有效的管理员 Bearer Token |

调用示例：

```http
POST /dev/flame/admin/api/product/distribute
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "id": 31,
  "decision": "rejected"
}
```

### 8.2 请求参数与状态规则

| 字段 | 类型 | 是否必填 | 规则与说明 |
| --- | --- | --- | --- |
| `id` | `integer` | 是 | 待发放列表返回的积分兑换流水主键 `point_record.id`，必须大于 `0` |
| `decision` | `string` | 否 | `distributed` 表示确认发放，`rejected` 表示拒绝发放；省略时默认为 `distributed`，兼容原请求 |

只有同时满足以下条件的流水可以接受发放审核：

```text
point_record.change_type = 'exchange'
point_record.product_id IS NOT NULL
point_record.status = 1
point_record.gift_distribution_status IN ('pending', 'distributed', 'rejected')
```

审核规则如下：

- **确认发放**：将 `pending` 更新为 `distributed`，不修改积分或描述。
- **拒绝发放**：将 `pending` 更新为 `rejected`，把原流水 `description` 更新为 `发放失败，请联系管理员`，并新增一条 `exchange_refund` 积分流水。
- **结果通知**：首次进入 `distributed` 或 `rejected` 时创建标题为 `奖品发放结果` 的 `pending` 通知。
- **重复结论**：对相同终态按幂等成功处理，不重复写状态、退款或通知。
- **结论冲突**：`distributed` 与 `rejected` 不能互相覆盖，返回 `409 Conflict`。

拒绝时退还积分等于原兑换流水 `change_points` 的绝对值。新退款流水的 `points_after` 以该用户最新有效积分流水余额加退款积分计算，`description` 为 `礼品拒绝发放，退还兑换积分`。兼容历史数据中 `change_points = 0` 的兑换流水：此时仍记录零积分退款，但不得根据商品当前价格凭空增加余额。

确认发放通知保存发放结果、奖品名称和兑换时间。拒绝发放通知在此基础上增加退还积分和处理说明。

### 8.3 成功响应

状态码：`200 OK`。

```json
{
  "id": 31,
  "gift_distribution_status": "rejected"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `integer` | 已处理的积分兑换流水主键 |
| `gift_distribution_status` | `string` | 最终审核状态：`distributed` 或 `rejected` |

首次处理和重复提交相同审核结论均返回相同响应。

### 8.4 异常处理

| 场景 | 状态码 | `detail` 或处理方式 |
| --- | --- | --- |
| 请求体缺少 `id`、ID 不大于 `0` 或 `decision` 非法 | `422 Unprocessable Entity` | FastAPI 参数校验错误 |
| 流水不存在 | `404 Not Found` | `兑换流水不存在` |
| 流水不是有效的商品兑换记录 | `409 Conflict` | `该流水不是有效的商品兑换记录` |
| 已有终态与本次结论冲突或状态异常 | `409 Conflict` | `礼品发放状态异常，无法更新` |
| 原兑换 `change_points` 为正数、用户不存在或最新积分流水缺失 | `409 Conflict` | `用户积分流水不完整，无法拒绝发放` |
| 管理员 Token 缺失、无效或过期 | `303 See Other` | 重定向至管理端登录接口 |
| 数据库读取或写入失败 | `500 Internal Server Error` | 当前事务回滚，不暴露 SQL 或连接信息 |

---

## 9. 数据、事务与安全边界

数据事实来源：

- [积分变动记录表说明](../db/point-record.md)
- [商品表说明](../db/product.md)
- [用户通知表说明](../db/notification.md)

代码入口：

- `app/router/product.py`
- `app/services/products.py`
- `app/repositories/products.py`
- `app/router/__init__.py`

三个查询用例均在显式只读事务中执行单次查询。商品列表读取 `product` 的全部字段并保留上下架商品；待发放列表不联表读取用户或商品详情；奖品信息接口按主键直接读取 `product`，不根据上下架状态隐藏历史商品。

奖品新增会先在事务外校验 WebP，再在单一写事务内创建默认上架的商品记录。数据库提交后才复用客户端 `/product/replace` 落盘图片；因此数据库写入失败不会产生图片文件，但客户端失败会形成商品已创建而图片尚未落盘的部分成功状态。

商品可见状态修改在单一写事务中使用 `FOR UPDATE` 锁定目标商品，再参数化覆盖 `product.status`。操作不触碰积分流水及其他商品字段；目标商品不存在时不执行更新，异常退出会回滚事务。

商品基础资料修改会先在事务外限量读取并校验可选 WebP 图片，再锁定 `product.id`，仅以请求中的显式字段构建最终值。提交积分字段时，在同一写事务内先校验激活赛季配置窗口；配置窗口异常、商品不存在或数据库写入失败都会回滚，并且不会调用客户端图片接口。

图片变更是刻意设计的跨系统非原子流程：数据库提交后才读取旧图共享引用并调用客户端 `/product/replace`。该顺序保证客户端不会在数据库失败前删除旧文件，但也意味着外部调用失败时数据库已经保存新地址。服务不自动重试这个带文件删除副作用的请求；管理端应依据 `502` 的部分成功提示重新加载数据后人工处理。

确认发放在单一事务内锁定目标兑换流水、更新终态并创建通知。拒绝发放按“用户主记录、目标兑换流水、最新有效积分流水”的顺序加锁，然后在同一事务内更新原流水、新增退款流水并创建通知，避免同一用户多笔拒绝退款并发覆盖 `points_after`。

拒绝流程不会改写原兑换流水的 `change_points`、`points_after` 或 `status`。原记录状态更新、失败提示、新退款流水和通知必须全部成功才提交，任一步失败都会整体回滚。

---

## 10. 验证方式

在仓库根目录执行：

```bash
python -m unittest tests.test_product_pending_distribution -v
python -m unittest tests.test_product_info -v
python -m unittest tests.test_product_list -v
python -m unittest tests.test_product_creation -v
python -m unittest tests.test_product_status -v
python -m unittest tests.test_product_update -v
python -m unittest tests.test_product_distribution -v
```

测试覆盖完整商品字段、上下架商品、稳定排序、空商品列表、奖品新增及默认上架、可空描述、商品状态行锁与幂等更新、商品基础资料局部更新、描述清空、严格字段校验、积分配置窗口、WebP 真格式校验、唯一图片地址、multipart 图片存储与替换请求、数据库先提交顺序、共享旧图保护、图片部分成功错误、商品不存在、兑换时间和待发放字段映射、奖品信息映射、确认发放通知、拒绝退款通知、用户级锁、终态幂等与冲突、异常积分、通知失败回滚、错误映射和管理员认证。

---

## 11. 已知限制

- 当前按需求返回全部待发放礼品，尚未提供分页；数据量增长后应增加分页参数。
- 商品列表当前返回全部商品且尚未提供分页、筛选或业务排序；商品规模增长后应根据管理前端需求增加查询参数。
- 奖品新增和图片替换的客户端调用都位于数据库提交之后，无法与 MySQL 事务形成原子提交；客户端失败时需要依据 `502` 提示重新加载并人工处理。
- 待发放列表只返回用户 ID 和商品 ID；用户名称可通过用户批量查询接口获取，商品信息可通过本文件的 `/product/info` 接口获取。
- 当前数据表没有独立的审核意见、处理时间与操作人字段，因此只能通过终态、固定描述和退款流水追踪结果，不能提供完整的履约审计明细。
- 部署接口代码前必须先根据数据库现状执行对应的三态迁移脚本；应用不会自动执行 DDL。
