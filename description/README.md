# 燃动现象项目文档地图

> **地图定位**
>
> 本文档负责回答“项目有哪些文档、某类任务应该去哪里阅读”。业务规则以 [项目总览](project.md) 和对应功能文档为准，写作格式以 [项目文档撰写规范](document-style.md) 为准；本文档不重复定义业务事实。

## 1. 快速开始

开始开发、审查、排障或维护文档前，按以下顺序建立上下文：

1. 阅读根目录 [AGENTS.md](../AGENTS.md)，确认必须遵守的开发流程和安全边界。
2. 阅读 [项目文档撰写规范](document-style.md)，了解所有文档的结构与格式要求。
3. 阅读 [项目总览](project.md)，理解项目定位、客户端玩法、管理端职责和关键业务口径。
4. 使用本文档地图定位本次任务涉及的功能文档和数据库文档。
5. 阅读目标代码、相邻模块、调用方、测试和配置，核对文档与实际实现。

> **注意**
>
> `project.md` 是项目总览，`db/project.md` 是运动项目数据表说明。两者名称相近但职责完全不同，不能互相替代。

---

## 2. 文档目录总览

当前文档目录与后续按需扩展的结构如下：

```text
description/
├── README.md                 # 文档地图
├── project.md                # 项目定位、业务总览与阅读规则
├── document-style.md         # Markdown 文档撰写规范
├── db/                       # 数据库表说明，默认只读
├── api/                      # 管理 API 文档
├── application/              # 应用用例与事务编排文档
├── domain/                   # 核心业务规则与领域设计文档
├── infrastructure/           # 数据访问、运行依赖和外部集成文档
├── job/                      # 定时任务与异步任务文档
└── features/                 # 跨层完整功能总览，按需创建
```

目录状态说明：

| 路径 | 当前状态 | 说明 |
| --- | --- | --- |
| `description/` | 已存在 | 存放项目级入口文档 |
| `description/db/` | 已存在 | 存放数据库结构和字段语义说明 |
| `description/api/` | 已存在 | 存放管理 API 契约和接口验证方式 |
| `description/application/` | 已存在 | 存放应用服务职责、用例流程与事务编排文档 |
| `description/domain/` | 已存在 | 存放赛季生命周期等核心业务规则与状态流转文档 |
| `description/infrastructure/` | 已存在 | 存放运行依赖、数据访问和外部系统集成文档 |
| `description/job/` | 已存在 | 存放赛季状态检查等定时任务文档 |
| `description/features/` | 按需创建 | 一个功能横跨多个层级、需要统一导航时创建 |

> **说明**
>
> 尚未产生真实内容的目录不会提前创建。上面的目录树描述目标组织方式，不表示所有目录目前都已经存在。

---

## 3. 项目级入口文档

项目级文档用于建立全局上下文，应先于具体功能文档阅读。

| 文档 | 主要内容 | 阅读时机 |
| --- | --- | --- |
| [仓库 README](../README.md) | 项目定位、已实现能力、架构、启动、配置、验证和部署入口 | 第一次接触或运行项目时 |
| [AGENTS.md](../AGENTS.md) | 智能体开发规范、函数中文注释、测试、安全和数据库只读要求 | 每次开始辅助开发前 |
| [项目总览](project.md) | 产品定位、核心玩法、管理端边界、逻辑架构、业务冲突与待确认事项 | 每次开始项目任务前 |
| [项目文档撰写规范](document-style.md) | 标题、列表、强调、引用、表格、代码块、链接和图表规范 | 新增或修改任何文档前 |
| [项目文档地图](README.md) | 文档目录、任务导航和地图维护规则 | 定位任务所需资料时 |

---

## 4. 数据库文档地图

`description/db/` 是数据库结构、字段语义、索引和关联关系的事实来源。除非用户明确要求优化或修改数据库文档，否则该目录**只读**。

### 4.1 组织、用户、通知与反馈

| 数据表 | 文档 | 主要用途 |
| --- | --- | --- |
| `department` | [部门表说明](db/department.md) | 企业部门、用户归属和排行榜部门展示 |
| `user` | [用户表说明](db/user.md) | 用户基础信息、部门归属、头像和身高 |
| `notification` | [用户通知表说明](db/notification.md) | Markdown 工作通知及钉钉投递状态 |
| `user_suggestion` | [用户建议表说明](db/user-suggestion.md) | 用户建议、记录状态和处理阶段 |

### 4.2 运动项目与挑战配置

| 数据表 | 文档 | 主要用途 |
| --- | --- | --- |
| `project` | [运动项目表说明](db/project.md) | 运动项目基础信息和启停状态 |
| `project_level` | [挑战等级表说明](db/project-level.md) | 挑战等级、奖励积分和启停状态 |
| `project_rule` | [项目规则表说明](db/project-rule.md) | 项目与等级对应的挑战规则 |
| `project_upload_config` | [项目上传配置表说明](db/project-upload-config.md) | 凭证类型、上传提示和展示顺序 |

### 4.3 赛季参与、凭证与排行榜

| 数据表 | 文档 | 主要用途 |
| --- | --- | --- |
| `season` | [赛季表说明](db/season.md) | 赛季日期、要求项目数量和赛季状态 |
| `season_user` | [赛季用户表说明](db/season-user.md) | 用户参与、统一挑战等级和最终积分 |
| `season_user_project` | [赛季用户项目表说明](db/season-user-project.md) | 用户锁定项目和项目完成进度 |
| `proof_record` | [凭证记录表说明](db/proof-record.md) | 运动凭证、审核状态和进度贡献 |
| `season_supplement_eligibility` | [赛季补传资格表说明](db/season-supplement-eligibility.md) | 结算中赛季允许用户补传的凭证记录 |
| `leaderboard_snapshot` | [排行榜快照表说明](db/leaderboard-snapshot.md) | 当前赛季最新排行榜快照 |

### 4.4 积分与商品

| 数据表 | 文档 | 主要用途 |
| --- | --- | --- |
| `point_record` | [积分变动记录表说明](db/point-record.md) | 用户跨赛季积分变动、兑换礼品履约三态和变动后余额 |
| `product` | [商品表说明](db/product.md) | 积分商城商品、兑换积分和上下架状态 |

> **警告**
>
> 数据库文档之间或与当前业务规则存在冲突时，不得通过直接修改 `description/db/` 消除冲突。应先查阅 [项目总览中的待确认事项](project.md)，再向用户说明影响并请求确认。

---

## 5. API 与基础设施文档

### 5.1 API 文档

| 文档 | 主要内容 |
| --- | --- |
| [服务健康检查 API](api/health.md) | 管理服务存活接口、响应结构和边界语义 |
| [管理端密钥认证 API](api/admin-authentication.md) | 管理员密钥换取短期 token、统一鉴权和内存缓存边界 |
| [赛季管理 API 路由](api/season.md) | 获取全部赛季列表，校验时间边界与可见项目容量并创建未开始赛季 |
| [赛季统计 API 路由](api/season-statistics.md) | 赛季聚合查询的统一路由边界、扩展规则和当前限制 |
| [赛季结算 API 路由](api/settlement.md) | 查询结算赛季、用户详情和待终审队列，执行结算终审、积分发放及一键赛季收口 |
| [用户基础信息 API](api/user.md) | 按用户 ID 批量获取名称、部门名称和头像地址 |
| [用户意见 API](api/suggestion.md) | 拉取可见且待处理的意见，并将其标记为拒绝或已解决 |
| [图片安全中转 API](api/image.md) | 经客户端后端安全读取用户头像、项目图标、商品图片、运动凭证和活动海报，并中转替换固定海报 |
| [运动项目管理 API](api/project.md) | 获取或创建项目、读取规则内容，并在赛季配置窗口内修改项目可见状态 |
| [挑战等级管理 API](api/project-level.md) | 获取全部挑战等级，在赛季配置窗口内创建等级、初始化项目规则、修改奖励积分及按既有标签配置规则值 |
| [积分商城商品 API](api/product.md) | 新增带 WebP 图片的奖品，获取完整商品列表，局部修改商品资料与图片，切换上下架状态，查询待发放兑换流水与奖品信息，并处理发放或拒绝退款 |
| [凭证终审 API](api/proof.md) | 查询待终审凭证，并原子记录终审结果、进度回退与回补 |

### 5.2 基础设施文档

| 文档 | 主要内容 |
| --- | --- |
| [FastAPI 应用结构与基础连接](infrastructure/application-structure.md) | 应用目录、配置、MySQL 会话、客户端后端连接、启动和测试 |
| [客户端立即初审集成](infrastructure/client-preliminary-review.md) | 结算遗留凭证立即初审的内部 HTTP 契约、失败语义和安全边界 |
| [Python 运行依赖](infrastructure/python-dependencies.md) | Python 环境基线、依赖用途、安装、验证和安全要求 |
| [管理端 Docker Compose 部署](infrastructure/docker-compose-deployment.md) | 管理端后端镜像、Compose 服务拓扑、根环境变量、启动顺序和生产入口 |

### 5.3 应用服务文档

| 文档 | 主要内容 |
| --- | --- |
| [应用服务层设计](application/service-layer.md) | 路由、服务、仓储与客户端的职责边界，事务和异常编排规则 |
| [赛季结算应用编排](application/season-settlement.md) | 状态初始化、遗留初审、资格、定分、发放及手动或自动收口的事务边界 |

### 5.4 领域文档

| 文档 | 主要内容 |
| --- | --- |
| [赛季生命周期](domain/season-lifecycle.md) | 赛季四态定义、正常流转、查询边界和历史数据迁移规则 |
| [赛季结算规则](domain/season-settlement.md) | 用户分组、补传资格、基础积分与连续完成奖励 |

### 5.5 定时任务文档

| 文档 | 主要内容 |
| --- | --- |
| [赛季状态与结算定时任务](job/season-status-transition.md) | 按上海业务日期推进状态、持续常规结算并在配置期限后自动收口 |

---

## 6. 按开发任务导航

下表只用于快速定位资料。业务任务跨越多个领域时，应合并阅读对应行中的文档，并继续追踪直接关联的表和代码。

| 开发任务 | 首要业务入口 | 相关数据库文档 |
| --- | --- | --- |
| 管理端认证与 token | [管理端密钥认证 API](api/admin-authentication.md)、[应用服务层设计](application/service-layer.md) | 无 |
| 部门与用户查询 | [用户基础信息 API](api/user.md) | [部门表](db/department.md)、[用户表](db/user.md) |
| 用户头像读取 | [图片安全中转 API](api/image.md) | [用户表](db/user.md) |
| 项目图标读取 | [图片安全中转 API](api/image.md) | [项目表](db/project.md) |
| 商品图片读取 | [图片安全中转 API](api/image.md)、[积分商城商品 API](api/product.md) | [商品表](db/product.md) |
| 运动凭证图片读取 | [图片安全中转 API](api/image.md)、[凭证终审 API](api/proof.md) | [凭证记录表](db/proof-record.md)、[赛季用户表](db/season-user.md)、[赛季表](db/season.md) |
| 活动海报读取与替换 | [图片安全中转 API](api/image.md) | 无 |
| 业务结果通知 | [通知写入规则](application/result-notifications.md)、[赛季结算应用编排](application/season-settlement.md)、[凭证终审 API](api/proof.md)、[礼品发放 API](api/product.md) | [用户通知表](db/notification.md)、[赛季用户表](db/season-user.md)、[凭证记录表](db/proof-record.md)、[积分流水表](db/point-record.md)、[用户表](db/user.md) |
| 用户反馈管理 | [用户意见 API](api/suggestion.md)、[管理端职责边界](project.md) | [用户建议表](db/user-suggestion.md)、[用户表](db/user.md) |
| 运动项目查询、创建、状态与规则展示 | [运动项目管理 API](api/project.md) | [项目表](db/project.md)、[挑战等级表](db/project-level.md)、[项目规则表](db/project-rule.md)、[上传配置表](db/project-upload-config.md) |
| 运动项目配置管理 | [运动项目管理 API](api/project.md)、[项目与挑战规则](project.md) | [项目表](db/project.md)、[挑战等级表](db/project-level.md)、[项目规则表](db/project-rule.md)、[上传配置表](db/project-upload-config.md) |
| 挑战等级与规则配置管理 | [挑战等级管理 API](api/project-level.md)、[项目与挑战规则](project.md) | [挑战等级表](db/project-level.md)、[项目规则表](db/project-rule.md)、[赛季用户表](db/season-user.md) |
| 赛季管理 | [赛季管理 API 路由](api/season.md)、[赛季生命周期](domain/season-lifecycle.md)、[赛季状态与结算定时任务](job/season-status-transition.md)、[赛季规则](project.md) | [赛季表](db/season.md)、[赛季用户表](db/season-user.md)、[赛季用户项目表](db/season-user-project.md) |
| 赛季统计查询 | [赛季统计 API 路由](api/season-statistics.md) | [赛季表](db/season.md)、[赛季用户表](db/season-user.md)、[赛季用户项目表](db/season-user-project.md)、[凭证记录表](db/proof-record.md) |
| 报名或参与记录 | [参与赛季规则](project.md) | [赛季表](db/season.md)、[赛季用户表](db/season-user.md)、[赛季用户项目表](db/season-user-project.md) |
| 凭证查询与终审 | [凭证终审 API](api/proof.md)、[凭证审核规则](project.md) | [凭证记录表](db/proof-record.md)、[赛季用户项目表](db/season-user-project.md)、[项目规则表](db/project-rule.md) |
| 进度回退与回补 | [进度规则](project.md) | [凭证记录表](db/proof-record.md)、[赛季用户项目表](db/season-user-project.md) |
| 排行榜 | [排行榜规则](project.md) | [排行榜快照表](db/leaderboard-snapshot.md)、[凭证记录表](db/proof-record.md)、[赛季用户表](db/season-user.md)、[用户表](db/user.md)、[部门表](db/department.md) |
| 赛季结算 | [赛季结算 API 路由](api/settlement.md)、[赛季结算规则](domain/season-settlement.md)、[赛季结算应用编排](application/season-settlement.md)、[赛季状态与结算定时任务](job/season-status-transition.md)、[客户端立即初审集成](infrastructure/client-preliminary-review.md) | [赛季表](db/season.md)、[赛季用户表](db/season-user.md)、[赛季用户项目表](db/season-user-project.md)、[凭证记录表](db/proof-record.md)、[赛季补传资格表](db/season-supplement-eligibility.md)、[挑战等级表](db/project-level.md) |
| 积分管理 | [积分结算与商城规则](project.md) | [积分流水表](db/point-record.md)、[用户表](db/user.md) |
| 商品管理与礼品发放 | [积分商城商品 API](api/product.md)、[积分商城规则](project.md) | [商品表](db/product.md)、[积分流水表](db/point-record.md) |
| 环境安装或依赖维护 | [Python 运行依赖](infrastructure/python-dependencies.md) | 无 |
| 应用启动、服务分层、MySQL 或客户端后端连接 | [FastAPI 应用结构与基础连接](infrastructure/application-structure.md)、[应用服务层设计](application/service-layer.md) | 按具体业务继续选择 |
| Docker Compose 构建与部署 | [管理端 Docker Compose 部署](infrastructure/docker-compose-deployment.md)、[FastAPI 应用结构与基础连接](infrastructure/application-structure.md) | 无 |

“首要业务入口”默认指向项目总览；具体功能文档产生后，应在本表中优先链接到更精确的功能文档。

---

## 7. 功能文档目录职责

后续功能开发完成后，根据其所在层级选择文档位置。

| 目录 | 适合记录的内容 | 不应记录的内容 |
| --- | --- | --- |
| `api/` | 管理 API 路径、认证、请求、响应、错误码和示例 | 核心业务算法的唯一实现说明 |
| `application/` | 用例流程、事务边界、依赖编排和失败处理 | 具体 ORM 或第三方协议细节 |
| `domain/` | 业务术语、实体、不变量、状态流转和计算规则 | HTTP 请求格式或部署命令 |
| `infrastructure/` | 数据访问、模型服务、文件服务和其他外部适配 | 与基础设施无关的产品规则 |
| `job/` | 触发条件、处理范围、并发、幂等、重试和监控 | 普通同步接口说明 |
| `features/` | 跨层功能目标、端到端流程和子文档导航 | 复制各层文档的全部细节 |

一个功能横跨多个层级时，应把规则放到对应层级文档，再由 `features/` 下的总览文档串联。不要把所有内容堆进单个超长文件，也不要在多个文档中重复维护同一事实。

---

## 8. 地图维护规则

### 8.1 新增文档时

新增功能文档必须完成以下操作：

1. 按 [项目文档撰写规范](document-style.md) 编写文档。
2. 将文档放入职责匹配的目录，不创建无内容的空目录。
3. 在本文档的对应分类或任务导航中增加链接。
4. 检查关联文档中的反向链接和术语是否仍然准确。
5. 验证所有仓库内相对链接能够正确定位。

### 8.2 移动、重命名或删除文档时

移动、重命名或删除非数据库文档时，必须同步更新：

- 本文档中的目录树、分类表和任务导航；
- [项目总览](project.md) 中的阅读规则和文档索引；
- 其他功能文档中的交叉引用；
- 代码注释或配置中存在的文档路径。

`description/db/` 中的文件没有用户明确授权时，不得移动、重命名或删除。

### 8.3 文档地图完成标准

每次文档变更完成后，确认：

- [ ] 新文档能够从本文档地图中找到。
- [ ] 文档位于职责匹配的目录。
- [ ] 链接名称能够说明目标内容，而不是使用“详情”或“点击这里”。
- [ ] 所有相对链接均有效。
- [ ] 没有把待创建目录误写成已经存在。
- [ ] 没有复制其他文档维护的完整业务规则。
- [ ] 没有未经授权修改 `description/db/`。

> **完成标准**
>
> 第一次接触项目的开发者应能从本文档出发，在一分钟内定位项目总览、写作规范、目标功能文档和相关数据库表说明。
