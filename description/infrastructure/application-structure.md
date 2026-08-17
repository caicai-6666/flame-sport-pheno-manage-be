# FastAPI 应用结构与基础连接

> **文档目的**
>
> 本文档说明管理端后端初始化后的 FastAPI 目录结构、HTTP Schema 分层、环境配置、异步 MySQL 会话和客户端后端 HTTP 连接边界，为后续业务模块开发提供统一入口。

## 1. 应用结构

当前代码结构如下：

```text
app/
├── __init__.py
├── main.py                       # FastAPI 应用工厂、生命周期和启动入口
├── schemas/
│   ├── __init__.py               # HTTP Schema 包声明，不集中重导出模型
│   ├── admin_auth.py             # 管理员认证请求与响应结构
│   ├── health.py                 # 健康检查响应结构
│   ├── product.py                # 商品与礼品履约请求、响应及字段校验
│   ├── project.py                # 项目请求、响应及 multipart JSON 适配器
│   ├── project_level.py          # 挑战等级与规则配置请求、响应
│   ├── proof.py                  # 凭证查询与终审请求、响应
│   ├── season.py                 # 赛季管理请求与响应
│   ├── season_statistics.py      # 赛季统计响应结构
│   ├── suggestion.py             # 用户意见请求与响应
│   └── user.py                   # 用户查询参数类型与响应结构
├── router/
│   ├── __init__.py               # 公开与受保护路由聚合
│   ├── dependencies.py           # 管理路由统一认证依赖
│   ├── admin_auth.py             # 管理员密钥换取 token
│   ├── health.py                 # 服务存活检查
│   ├── image.py                  # 客户端后端图片安全中转
│   ├── product.py                # 商品列表、资料修改、上下架、礼品查询与发放审核路由
│   ├── project.py                # 项目查询、创建、可见状态与等级规则接口
│   ├── project_level.py          # 挑战等级、奖励积分与项目规则配置路由
│   ├── proof.py                  # 待终审凭证查询与终审写入路由
│   ├── season.py                 # 赛季查询与创建路由
│   ├── season_statistics.py      # 赛季统计子路由
│   ├── suggestion.py             # 待处理用户意见查询与处理路由
│   └── user.py                   # 用户基础信息查询路由
├── clients/
│   ├── __init__.py
│   └── client_backend.py         # 客户端后端 HTTP 适配器
├── core/
│   ├── __init__.py
│   ├── admin_auth.py             # token 签发、摘要与进程内缓存
│   └── config.py                 # 环境配置加载与校验
├── db/
│   ├── __init__.py
│   └── session.py                # 异步 MySQL 引擎和会话工厂
├── services/
│   ├── __init__.py
│   ├── admin_auth.py             # 管理员登录用例
│   ├── configuration_guard.py    # 激活赛季高影响配置变更窗口守卫
│   ├── images.py                 # 图片中转、项目图标上传与商品图片替换适配
│   ├── products.py               # 商品查询、资料与状态修改、礼品审核及拒绝退款用例
│   ├── project_levels.py         # 挑战等级、奖励积分与项目规则配置用例
│   ├── projects.py               # 项目查询、创建、可见状态修改与等级规则用例
│   ├── proofs.py                 # 待终审查询与终审进度编排用例
│   ├── seasons.py                # 赛季查询、创建与状态含义映射用例
│   ├── season_statistics.py      # 当前赛季统计查询用例
│   ├── suggestions.py            # 待处理用户意见查询与处理用例
│   └── users.py                  # 用户基础信息查询用例
└── repositories/
    ├── __init__.py
    ├── configuration_guard.py    # 激活赛季配置窗口共享锁定查询
    ├── products.py               # 商品与礼品查询、资料、状态和退款持久化
    ├── project_levels.py         # 挑战等级查询、创建与奖励积分更新
    ├── projects.py               # 项目查询、项目及子配置持久化
    ├── proofs.py                 # 待终审查询、行锁与进度写入
    ├── seasons.py                # 赛季查询、日期边界锁定与创建持久化
    ├── season_statistics.py      # 当前赛季、正式参赛人员与项目进度查询
    ├── suggestions.py            # 待处理意见查询、处理行锁与阶段写入
    └── users.py                  # 用户与部门基础信息查询

tests/
├── __init__.py
├── test_active_season_config.py # 激活赛季配置变更窗口校验测试
├── test_admin_auth.py            # 管理员认证与 token 缓存测试
├── test_configuration_guard.py  # 激活赛季配置窗口与锁定查询测试
├── test_current_season_statistics.py # 当前赛季接口与仓储测试
├── test_database_session.py      # 请求级数据库会话依赖测试
├── test_health.py                # 基础应用生命周期与健康接口测试
├── test_image_avatar.py          # 头像、项目图标、商品与凭证图片安全中转测试
├── test_project_level.py         # 挑战等级创建、查询、积分修改与事务测试
├── test_project_rule_update.py   # 项目等级规则局部更新、窗口与行锁测试
├── test_project_list.py          # 可见项目列表接口与仓储测试
├── test_project_creation.py      # 项目创建、规则矩阵、WebP 上传与事务测试
├── test_project_rule.py          # 项目等级规则接口、服务与仓储测试
├── test_project_status.py        # 项目可见状态、配置窗口与行锁测试
├── test_product_pending_distribution.py # 待发放礼品接口、服务与仓储测试
├── test_product_info.py          # 奖品信息接口、服务与仓储测试
├── test_product_list.py          # 完整商品列表接口、服务与仓储测试
├── test_product_creation.py      # 奖品新增、WebP 落盘与部分成功测试
├── test_product_status.py        # 商品上下架、行锁、幂等与认证测试
├── test_product_update.py        # 商品资料补丁、配置窗口与图片替换顺序测试
├── test_product_distribution.py  # 礼品发放状态、幂等与并发保护测试
├── test_proof.py                 # 待终审凭证接口、服务与仓储测试
├── test_proof_final_review.py    # 终审状态、进度回退与回补测试
├── test_season_statistics_router.py # 受保护路由聚合测试
├── test_schema_boundaries.py     # HTTP Schema 放置与依赖方向约束测试
├── test_services.py              # 应用服务编排、事务和异常测试
├── test_suggestion.py            # 待处理意见接口、服务与仓储测试
├── test_suggestion_processing.py # 意见处理接口、行锁、幂等与冲突测试
└── test_user_info.py             # 用户基础信息接口与仓储测试

script/
├── migrate-point-record-gift-distribution-rejected.sql
│                                 # 已有两态礼品状态升级为三态的增量脚本
├── migrate-point-record-gift-distribution-status.sql
│                                 # 首次增加礼品履约三态的生产迁移脚本
└── migrate-user-suggestion-status-and-processing-stage.sql
                                  # 用户建议表一次性生产迁移脚本
```

各目录职责如下：

| 目录 | 职责 |
| --- | --- |
| `app/schemas/` | 定义 Pydantic HTTP 请求、响应、字段级约束及纯传输格式适配，不依赖路由、服务或仓储 |
| `app/router/` | 声明 HTTP 路径和参数位置，注入公共依赖，调用服务并映射响应与异常；不定义 Pydantic 模型或复杂业务规则 |
| `app/clients/` | 隔离客户端后端及后续其他外部服务的 HTTP 协议 |
| `app/core/` | 维护应用级配置及不属于具体业务域的核心能力 |
| `app/db/` | 维护数据库引擎、连接池和请求级会话 |
| `app/services/` | 编排业务用例、事务、下层依赖和稳定应用异常，不处理 HTTP 协议 |
| `app/repositories/` | 集中维护业务数据访问和聚合 SQL，不处理 HTTP 协议 |
| `tests/` | 维护可使用项目既定命令运行的自动化测试 |

业务领域和数据库模型目录应在产生真实功能时按职责创建，不提前建立空目录。

HTTP 路由从 `app/schemas/` 引用对应业务的请求与响应结构，再调用 `app/services/` 中的应用用例。服务管理事务和跨依赖编排，并调用仓储或客户端适配器。赛季统计的数据查询集中在 `app/repositories/season_statistics.py`，不能把 Pydantic 模型、聚合 SQL、用例规则或事务直接堆入路由函数。完整边界参见 [应用服务层设计](../application/service-layer.md)。

`schemas` 按业务主题与路由保持同名模块，不建立横跨全部业务的 `requests.py` 或 `responses.py`。其校验范围仅包括类型、长度、枚举、字段组合和传输格式；需要数据库、当前时间、配置窗口或外部服务才能判断的规则必须留在服务层。`app/schemas/__init__.py` 不集中重导出模型，避免形成隐式公共 API 和循环依赖。

管理 API 聚合时明确区分：

- `health.router`：公开存活检查；
- `admin_auth.router`：公开的 `/auth/login` 登录入口及受保护的会话校验；
- `protected_router`：统一添加 Bearer Token 依赖，所有业务子路由必须接入此聚合器。

认证机制、环境配置和进程内缓存边界以 [管理端密钥认证 API](../api/admin-authentication.md) 为准。

---

## 2. 应用入口与生命周期

`app/main.py` 使用应用工厂组装 FastAPI、CORS 中间件和 `/flame/admin/api` 路由。模块同时导出标准 ASGI 对象：

```python
from app.main import app
```

应用生命周期负责：

1. 启动时创建可复用连接池的客户端后端 HTTP 客户端。
2. 将客户端保存在 `app.state.client_backend`，供后续依赖注入层获取。
3. 退出时关闭 HTTP 客户端连接池。
4. 退出时释放异步 MySQL 引擎的连接池。

启动阶段不会主动访问 MySQL 或客户端后端，因此缺少外部连接时仍可导入应用并执行存活检查。需要依赖可用性判断时，应由独立的就绪检查承担。

---

## 3. 环境配置

### 3.1 配置文件

仓库根目录包含：

| 文件 | 用途 | 是否提交版本库 |
| --- | --- | --- |
| `.env.example` | 配置项模板和本地开发示例 | 是 |
| `.env` | 当前机器的实际配置 | 否，已由 `.gitignore` 忽略 |

新环境应先复制模板，再填写真实配置：

```bash
cp .env.example .env
```

> **警告**
>
> `.env` 可能包含数据库密码、API Key 和内部服务地址，禁止提交到版本库、粘贴到日志或写入功能文档。

### 3.2 配置项

| 配置组 | 关键配置 | 说明 |
| --- | --- | --- |
| 应用 | `APP_NAME`、`APP_ENV`、`APP_DEBUG`、`TZ` | 服务名称、环境、调试开关和固定的 `Asia/Shanghai` 运行时区 |
| 监听 | `APP_HOST`、`APP_PORT` | Uvicorn 监听地址和端口 |
| API | `API_PREFIX`、`PUBLIC_API_PREFIX`、`CORS_ORIGINS` | 后端内部路由前缀、Nginx 对外路由前缀和允许访问的管理端前端来源 |
| 管理认证 | `ADMIN_KEY`、`ADMIN_TOKEN_TTL_SECONDS`、`ADMIN_TOKEN_CACHE_MAX_SIZE` | 管理员共享密钥、token 有效期和单进程缓存上限 |
| 赛季配置保护 | `ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` | 当前激活赛季开始后允许调整高影响业务配置的窗口，单位为小时 |
| MySQL | `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE` | 异步数据库连接信息 |
| 连接池 | `MYSQL_POOL_SIZE`、`MYSQL_MAX_OVERFLOW`、`MYSQL_POOL_RECYCLE_SECONDS` | 数据库连接池容量与回收周期 |
| 客户端后端 | `CLIENT_BACKEND_BASE_URL`、`CLIENT_BACKEND_TIMEOUT_SECONDS` | 客户端后端局域网服务地址和请求超时 |
| 图片缓存 | `IMAGE_CACHE_SECONDS` | 所有图片响应的浏览器私有缓存时效，单位为秒 |
| DeepSeek 模型 | `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_HTTP_TIMEOUT_SECONDS` | 与客户端后端共享的 OpenAI 兼容模型密钥、地址、模型名称和调用超时 |

配置由 `pydantic-settings` 从根目录 `.env` 和进程环境变量加载并校验。数据库密码使用 `SecretStr` 保存，构造连接地址时通过 SQLAlchemy `URL` 处理特殊字符。

客户端后端使用一个环境变量保存协议、局域网服务名、端口和管理接口基础路径：

```dotenv
CLIENT_BACKEND_BASE_URL=http://backend:8000/flame/api/admin
```

其中 `backend` 由当前局域网或容器网络提供名称解析。应用启动时使用标准 HTTP URL 类型校验配置；后续客户端后端接口路径均应相对于 `/flame/api/admin` 拼接，不再经过公网域名或 Nginx 的开发前缀。

DeepSeek 使用与客户端后端相同的环境变量名称和默认口径：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_HTTP_TIMEOUT_SECONDS=60
```

管理端目前只完成配置接入，不会因存在密钥而自动发起模型请求。后续实现具体模型用例时，应通过现有 `openai` SDK 的兼容客户端读取这些配置，并在独立客户端适配器中统一处理超时、有限重试和安全错误映射。

### 3.3 激活赛季配置变更窗口

`ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS` 统一定义当前激活赛季开始后仍可执行高影响配置变更的时长。当前赛季仍按 `season.status = 1` 识别；由于赛季表只保存日期，窗口起点统一解释为 `season.start_date` 在 `Asia/Shanghai` 时区的当日 `00:00`。业务代码显式使用该时区，不依赖宿主机或容器的默认时区。

该配置计划用于以下写操作：

- 修改挑战等级对应积分；
- 新建挑战等级；
- 新建运动项目；
- 修改运动项目配置；
- 修改奖品所需积分值。

示例配置：

```dotenv
ACTIVE_SEASON_CONFIG_EDIT_WINDOW_HOURS=24
```

配置必须是非负整数。`24` 表示从赛季开始时刻起保留 24 小时修改窗口；`0` 表示赛季激活后立即禁止上述修改。配置加载阶段会拒绝负数，避免各写接口形成不同的时间判断。

> **当前边界**
>
> 挑战等级创建、奖励积分修改、项目等级规则配置和项目可见状态修改已经接入共享窗口守卫。新建运动项目和修改奖品积分值等后续写接口仍需复用同一守卫，不得在各路由中重复实现时间判断。

---

## 4. 异步 MySQL 连接

`app/db/session.py` 使用以下组件：

- SQLModel 作为后续模型与数据访问基础；
- SQLAlchemy `AsyncEngine` 管理连接池；
- `asyncmy` 作为 MySQL 异步驱动；
- `AsyncSession` 作为请求级数据库会话。

FastAPI 路由或应用服务可以通过依赖注入获取会话：

```python
from app.db.session import DatabaseSession


# 仅在用例需要访问数据库时注入会话，事务边界由应用服务显式管理。
async def example_service(
    session: DatabaseSession,
) -> None:
    async with session.begin():
        ...
```

事务约束如下：

- 路由只有显式声明 `DatabaseSession` 时才会创建会话；通常到首次执行 SQL 时才从连接池获取连接。健康检查、登录等未声明该依赖的接口不会创建数据库会话。
- `get_session` 只负责创建和关闭请求级会话，不自动开启、提交或回滚业务事务。
- 当前业务接口由应用服务使用 `async with session.begin()` 管理事务，正常退出自动提交，异常自动回滚；路由只负责注入并传递会话。
- 需要多个明确提交阶段时，可以显式调用 `commit()` 和 `rollback()`；每个阶段必须说明一致性边界，不能把必须原子完成的业务拆开提交。
- 应用服务和仓储共享注入的同一个 `AsyncSession`，下层不得自行创建或关闭会话；是否允许分阶段提交由具体用例约定，默认由应用服务控制。
- 需要立即向数据库发送待持久化变更并读取数据库生成值时，可以调用 `flush()`，但 `flush()` 不等于提交。
- 终审、进度回补、结算和积分等跨表写操作必须在同一事务中完成。
- 长时间外部 HTTP、模型或文件调用不应放在数据库事务中；应先完成无事务准备，再进入明确的数据读写用例，避免长期占用连接和数据库锁。
- 当前初始化不会自动建表或修改表结构，数据库结构仍以 `description/db/` 为准。

> **警告**
>
> 涉及终审、进度回补、结算、积分或多表状态流转时，必须把所有关联写操作放入同一个明确事务。灵活提交不代表可以破坏业务原子性。

---

## 5. 客户端后端连接

`ClientBackendClient` 封装共享的 `httpx.AsyncClient`，负责：

- 统一使用 `CLIENT_BACKEND_BASE_URL` 作为客户端后端管理接口基础地址；
- 应用统一请求超时；
- 复用 TCP 连接池；
- 对非成功响应调用 `raise_for_status()`；
- 在应用退出时关闭连接池。

路由通过 `ClientBackend` 依赖从 `app.state.client_backend` 获取共享客户端并传给应用服务，不得在每个请求中重复创建 `httpx.AsyncClient`。当前图片中转服务只固定调用客户端后端 `/avator`、`/project_icon`、`/product` 与 `/proof_record/{id}`；项目创建使用固定的 `POST /project_icon` multipart 协议上传 WebP；奖品新增和商品资料修改先校验真实 WebP 并生成唯一地址，在数据库提交后使用固定的 `POST /product/replace` multipart 协议落盘新图并按需清理无共享引用的旧图。具体管理端协议参见 [运动项目管理 API](../api/project.md)与[积分商城商品 API](../api/product.md)。

该内部客户端设置 `trust_env=False`，不会隐式读取宿主机的 `HTTP_PROXY`、`HTTPS_PROXY` 或 SOCKS 代理。这样可以避免内部服务请求因开发机代理设置而改变路由；如果部署环境确实要求代理，应在明确安全边界后通过专门配置实现。

客户端后端必须监听其局域网网卡地址，且主机防火墙只允许管理端后端所在主机访问对应端口。环境变量限制了应用的目标地址，但不能替代客户端后端监听地址和防火墙访问控制。

> **注意**
>
> 当前图片 `/avator`、`/project_icon`、`/product` 与 `/proof_record/{id}` 读取接口、`POST /project_icon` 上传接口，以及 `POST /product/replace` 商品图片替换接口已有明确契约，并由图片服务处理状态与内容校验。后续新增客户端后端接口前，仍必须取得对应的路径、认证、超时、重试和错误协议，不能从已有图片接口自行推导。

---

## 6. CORS 边界

应用根据 `CORS_ORIGINS` 配置允许访问的管理端前端来源。该配置使用 JSON 数组，例如：

```dotenv
CORS_ORIGINS=["http://localhost:5173"]
```

生产环境必须填写明确来源，不能在允许携带凭证时使用通配符 `*`。当前前端通过 `Authorization: Bearer <token>` 携带令牌，因此允许跨域访问时必须确保 `Authorization` 请求头可用。

---

## 7. 启动与验证

### 7.1 Nginx 路径映射

开发环境以 `/dev` 区分环境，但该前缀只属于 Nginx 对外路由，不进入 FastAPI：

```text
外部请求  /dev/flame/admin/api/health
              │ Nginx 摘除 /dev
              ▼
后端请求  /flame/admin/api/health
```

因此，前端通过域名访问时使用 `/dev/flame/admin/api`，直接访问 `127.0.0.1:8001` 时使用 `/flame/admin/api`。FastAPI 不再注册旧的 `/api/v1` 前缀。

> **临时生产维护状态**
>
> 当前 Nginx 对生产前端入口 `/flame` 及其非 API 子路径直接返回 `503 Service Unavailable` 和文本 `升级改造中`，不再转发至生产前端服务。响应包含 `Retry-After: 3600`。生产 API `/flame/api`、内部管理接口和所有 `/dev` 开发路由保持原有规则；生产前端恢复上线时必须移除此临时返回规则并同步更新本文档。

### 7.2 启动服务

在仓库根目录执行：

```bash
uvicorn app.main:app --reload
```

也可以直接运行模块，此时监听配置读取 `.env`：

```bash
python -m app.main
```

启动后可访问：

- `GET /flame/admin/api/health`：服务存活检查；
- `POST /flame/admin/api/auth/login`：使用管理员密钥登录并换取短期访问令牌；
- `GET /flame/admin/api/auth/session`：验证 Bearer Token 是否有效；
- `/docs`：Swagger UI；
- `/redoc`：ReDoc。

### 7.3 执行测试

当前测试使用 Python 标准库 `unittest`，不需要额外测试依赖：

```bash
python -m unittest discover -s tests -v
```

执行语法编译检查：

```bash
python -m compileall -q app tests
```

数据库会话测试使用替身验证按需注入、异常传播和会话关闭，不修改开发数据库：

```bash
python -m unittest tests.test_database_session -v
```

---

## 8. 已知限制

- 已使用当前开发配置完成 MySQL 只读连通验证，但健康接口仍不表达数据库就绪状态。
- 除已经记录的图片读取、项目图标上传与商品图片替换接口外，其他客户端后端接口契约和服务间认证方式尚未确定。
- 当前只有共享密钥认证；管理员个人身份、角色权限和操作审计仍待确认。
- 尚未选择数据库迁移工具，初始化过程不会创建或改变数据库表。
- 当前健康接口只表达进程存活，不表达外部依赖就绪。

这些限制在对应需求确认前不得通过硬编码或猜测补全。
