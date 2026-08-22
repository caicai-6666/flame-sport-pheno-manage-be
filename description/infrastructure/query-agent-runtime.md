# 查询智能体运行时与业务域扩展

> **文档目的**
>
> 本文档说明 `app/agent/` 的通用引擎、工具、业务域配置、模型与数据库适配、安全边界及新增业务域的方法。

## 1. 包结构与依赖方向

```text
app/agent/
├── domains/       # 业务域 Profile、词汇、规则、表概述和实体检索配置
├── engine/        # 对齐、规划、SQL、翻译、审计子图和完整 Pipeline
├── events/        # 结构化进度模型、友好文案和 SSE 编码
├── interaction/   # 会话状态、交互暂停恢复和事件订阅
├── runtime/       # 模型选项、表结构缓存、结构读取和单表候选读取
├── tools/         # 通用 Pydantic 工具参数与 strict Schema 构造
└── query_manager.py
```

依赖方向为：

```text
HTTP router → application service → query manager → pipeline
                                               ├── common tools/runtime
                                               └── selected domain profile
```

通用工具不导入运动业务包。业务域通过 `QueryDomainProfile` 注入允许表、资源路径、展示名称和禁止在对齐结果中泄漏的数据库标识符。

---

## 2. 业务域配置契约

一个业务包至少包含：

```text
<domain>/
├── profile.py
├── business-alignment/
│   ├── table-overview.txt
│   └── business-vocabulary.txt
├── business-context/
│   └── core-game-rules.txt
├── table-context/
│   └── <one-file-per-allowed-table>.txt
└── entity-lookup.json
```

各资源职责如下：

| 资源 | 内容边界 |
| --- | --- |
| `table-overview.txt` | 只描述业务对象和数据特性，不提供表关系和字段细节 |
| `business-vocabulary.txt` | 用户词汇到标准业务概念的映射 |
| `core-game-rules.txt` | 对齐和规划共同使用的业务事实、推论和例外 |
| `table-context/*.txt` | 按依赖顺序描述表用途、行粒度、关系和查询不变量 |
| `entity-lookup.json` | 可命名实体的 ID、展示字段、匹配模式和相似度预算 |
| `profile.py` | 业务域名称、范围、表白名单、资源顺序和友好表名 |

Profile 加载时会检查允许表非空且不重复、表标签完整、每张允许表对应一个概述文件、资源文件存在，以及受保护数据库标识符非空。HTTP 请求只能选择显式注册表中的 `domain_key`，不能提供路径或动态导入位置。

新增积分等业务域时，应复制资源结构、按真实业务重新编写全部知识文件、建立独立 Profile，并在 `app/agent/domains/registry.py` 显式注册。通用引擎、工具协议、会话和 SSE 不应随业务域复制。

当前已注册的 `rewards` 业务域覆盖用户、部门、赛季参与、赛季结算积分、积分流水、商品和奖品履约。它与 `sports` 使用相同的查询流水线，但拥有独立的表白名单、业务词汇、核心规则、实体匹配配置和查询计划校验器。业务域专属 Prompt 规则通过 Profile 注入，通用 Prompt 不再硬编码运动凭证语义。

---

## 3. 模型调用约束

模型通过 OpenAI 兼容 SDK 调用 DeepSeek。工具型阶段使用 Beta strict function calling；全部阶段关闭供应商隐藏思考，并分别设置 `max_tokens`。生成轮次和工具次数另由 `AGENT_QUERY_*` 配置限制。

| 阶段 | 主要输出 | 工具策略 |
| --- | --- | --- |
| 业务对齐 | 标准业务需求或放弃理由 | 首轮 `think`，随后强制调用已注册工具 |
| 查询规划 | 完整结构化查询计划或放弃理由 | 首轮强制 `think`，后续强制工具调用 |
| 单表候选 | 一条单表 SELECT | 强制唯一 strict 工具 |
| SQL 生成 | 参数化 SQL 草稿 | 强制唯一 strict 工具，校验失败有限重试 |
| 翻译目标识别 | 待翻译结果列及直接来源 | 强制唯一 strict 工具，语义失败有限重试 |
| 注释映射提取 | 单字段原始值与展示值映射 | 每字段强制唯一 strict 工具，语义失败有限重试 |
| 结果审计 | 相关性、表格说明和统计摘要 | 强制唯一 strict 工具，参数错误有限重试 |

所有工具参数先由 Pydantic 校验。Schema 违规和业务校验失败以带原 `tool_call_id` 的正常工具结果加入模型上下文，反馈会指出错误字段、错误原因和唯一修复动作。

---

## 4. 模型上下文 YAML 契约

除 function calling 协议外，模型读取的结构化业务事实统一使用由 PyYAML 安全渲染的合法 YAML。业务对齐资源、核心规则、规划表概述、表结构、单表候选页、SQL 生成输入、翻译目标与单字段注释输入，以及结果审计输入均使用固定小写键，中文、多行 SQL、空值、布尔值和列表不通过手工字符串拼接。

每个模型阶段必须在稳定系统提示词中提供对应 YAML 键的中文含义。例如：

- `tables.table` 表示真实表名，`row_grain` 表示一行的业务含义；
- `columns.field_name`、`data_type`、`foreign_key` 和 `comment` 分别表示字段名、数据库类型、外键目标和数据库字段注释；
- `query_plan` 表示已确认的查询计划，`allowed_table_schemas` 表示允许 SQL 生成器读取的表结构；
- `rows_preview` 只是受限样本，`statistics` 才是基于完整结果计算的统计事实。

中文说明位于固定系统前缀，不在每次动态 YAML 中重复插入 `#` 行内注释。这样既保证字段语义明确，也避免注释与事实值混杂，并保持可复用的前缀缓存。动态内容仍必须能被 `yaml.safe_load` 解析；读取后还要由对应 Pydantic 模型或本地业务校验确认具体结构。

表结构成功结果固定为：

```yaml
table: user
columns:
  - field_name: id
    data_type: varchar(64)
    foreign_key: null
    comment: 用户ID
```

其中 `comment` 原样来自数据库结构，不由模型补写或改写。工具成功执行后回传给模型的事实同样使用 YAML；工具参数、strict Schema 和带原 `tool_call_id` 的失败反馈继续使用 JSON，这是供应商函数调用协议的一部分，不能改成 YAML。

---

## 5. 表结构与候选读取

`information_schema` 读取只接受业务域表白名单，返回字段名、类型、外键和备注。每个业务域在 `AgentQueryManager` 中拥有一个线程安全进程级缓存；规划、SQL 和翻译子图共用该读取器，只缓存成功结果，应用重启后清空。

> **注意**
>
> 数据库在线修改表结构后，当前进程可能继续使用旧缓存。管理端不自动迁移表结构，完成受控 DDL 后应重启应用，使结构缓存与数据库一致。

单表候选读取仅用于确认用户提到的数据库实际名称或筛选值，不用于预览最终业务结果。它具备以下约束：

- 目标表同时受远端工具枚举和本地白名单校验；
- SQLGlot AST 只允许一张物理表的 SELECT；
- 禁止 JOIN、子查询、CTE、通配列、变量赋值、锁、`SELECT INTO`、数据库限定名和高风险函数；
- 每页最多 `10` 行、最多 `8` 列、单元格与整页字符数受限；
- 模型不能控制 OFFSET，后续页面只能按内部 `inspection_id` 顺序读取；
- 精确名称未命中时，只对 `entity-lookup.json` 允许的实体执行固定预算相似候选计算；
- 数据库读取使用 `START TRANSACTION READ ONLY`、`10` 秒超时和结束回滚。

---

## 6. 最终 SQL 安全边界

最终 SQL 使用 SQLGlot 解析 MySQL AST，并在执行前验证：

1. 只有单条 SELECT 或最终为 SELECT 的 CTE 查询。
2. 基础表集合与规划层声明完全一致，且全部属于业务域白名单。
3. 禁止注释、分号、多语句、通配字段、锁、`SELECT INTO`、数据库限定名和高风险函数。
4. WHERE、HAVING 和 JOIN 条件中的外部标量必须使用命名占位符；参数集合必须精确匹配。
5. 输出列名必须唯一，并与声明的 `result_columns` 顺序一致。
6. `LIMIT` 和 `OFFSET` 必须精确服从规划结果；系统不会私自追加隐藏上限。

校验通过后，命名参数编译为 asyncmy 参数绑定，在 `START TRANSACTION READ ONLY` 中执行，超时为 `10` 秒，最终始终回滚并关闭连接。

运行时会同时保留两份语义等价的已校验 SQL：一份将命名参数编译为 `%s` 并仅供 asyncmy 绑定执行；另一份保留 `:parameter_name` 并仅供后续 AST 来源分析。两者均来自同一份通过表白名单、只读、字段、参数和分页校验的 SQL 草稿，不会执行第二条查询。

由于规划可按用户的完整导出或准确统计需求把 `limit` 设为 `null`，结果规模仍可能较大。部署时必须使用只读低权限数据库账号，并结合实际数据量监控查询时间、内存和返回体大小。

---

## 7. 结果翻译、整理与数据暴露

SQL 成功后先运行独立 `ResultTranslationSubgraph`：

1. 节点 1 根据保留命名占位符的已校验 SQL、直接列来源、无注释表结构和前 `5` 行识别状态、审核状态、类型、启停标记或布尔编码。ID、名称、日期、数值度量、聚合和已由 SQL 表达式转换的列不会进入翻译。
2. 节点 2 为每个目标字段分别构造只含一个 `comment` 的 YAML 上下文，以 `AGENT_QUERY_TRANSLATION_MAX_PARALLEL_FIELDS` 限制并发。不同字段共享固定系统前缀以利用前缀缓存，但彼此不能读取其他字段注释。
3. 节点 3 由本地程序把经过校验的映射应用到完整结果。模型映射中的原始值和展示值都必须能在该字段原始 `comment` 中找到；未知值和非目标字段保持原样。

翻译层保留 SQL 原始行，并另外生成翻译行。模型、工具协议或结构读取失败时，子图返回失败状态和原始行副本，Pipeline 继续进行结果整理，不因展示增强失败丢弃已经成功读取的数据。空结果不调用翻译模型。

程序根据翻译后或安全降级的实际结果列生成表头和完整 JSON 安全行，依据完整结果计算：

- 返回行数及是否达到规划上限；
- 低基数字符串、布尔或状态字段的类别分布；
- 排除 ID 和类别编码后的业务数值最小值与最大值。

审计模型只接收原问题、对齐结果、查询计划、程序统计和前 `5` 行结果样本。它不能重新统计样本或推断未展示行。HTTP 响应可以返回完整表格，因此前端仍需评估大结果的分页、下载或虚拟滚动策略。

---

## 8. SSE 与运行时边界

会话为每个订阅者创建独立 `asyncio.Queue`，模型线程通过线程安全回调向 FastAPI 事件循环投递。事件历史使用固定长度队列；`Last-Event-ID` 只能补发仍在历史窗口内的事件。心跳间隔由环境配置控制，响应设置 `X-Accel-Buffering: no`。

由于认证使用 Bearer Header，浏览器原生 `EventSource` 无法直接携带现有令牌。管理前端应使用支持流式读取的 `fetch` 客户端，或在未来建立同源安全 Cookie/一次性 SSE 票据后再使用 `EventSource`。禁止把长期 Bearer Token 放进 URL 查询参数。

当前会话和事件均为单进程内存实现，适配现有单 Worker 部署。多 Worker、多实例或需要进程重启恢复时，应将会话、交互和事件迁移到共享存储，并保持同一个事件序号与回答幂等协议。

---

## 9. 配置、验证与安全要求

相关配置分为模型连接、各阶段单次输出预算、翻译字段并发、生成与工具次数、活动会话、事件历史、会话保留和 SSE 心跳。完整变量见 [FastAPI 应用结构与基础连接](application-structure.md)。

自动化测试应替换模型与数据库，不依赖真实外部服务。受控联调只允许连接开发数据库并执行只读问题；日志和测试产物不得包含密钥、密码、原始 SQL、完整个人数据或模型原始响应。
