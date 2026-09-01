# 查询智能体运行时与业务域扩展

> **文档目的**
>
> 本文档说明 `app/agent/text2sql/` 的查询引擎、子图、工具、业务域配置、模型与数据库适配、安全边界及新增业务域的方法。

## 1. 包结构与依赖方向

```text
app/agent/
├── __init__.py
└── text2sql/
│   ├── domains/   # 业务域 Profile、词汇、规则、表概述和实体检索配置
│   ├── events/    # 结构化进度模型、友好文案和 SSE 编码
│   ├── function_calling/ # Function Calling Schema、参数解析与错误反馈算法
│   ├── interaction/ # 会话状态、交互暂停恢复和事件订阅
│   ├── shared/    # 模型选项、工具标签、系统指导和 YAML 上下文
│   ├── subgraphs/ # 对齐、规划、SQL、塑形、翻译和审计六个独立子图包
│   ├── diagnostics.py
│   ├── pipeline.py
│   └── query_manager.py
```

依赖方向为：

```text
HTTP router → application service → query manager → pipeline
                                               ├── Function Calling 基础设施
                                               ├── text2sql shared 上下文能力
                                               ├── selected subgraphs
                                               └── selected domain profile
```

当前查询会话、SSE 事件、交互等待、诊断日志和后台任务管理都直接依赖 Text-to-SQL 流水线，因此与业务域、稳定上下文能力和六个子图一起收口于 `app/agent/text2sql/`。`shared/` 只保留模型请求选项、工具标签模板、系统指导消息和 YAML 上下文；它不保存具体工具、表结构执行器或跨阶段塑形协议。`app/agent/` 根目录只保留智能体类型的命名空间，后续新增其他智能体业务时应建立新的同级业务包，不得把其运行时混入 `text2sql/`。业务域通过 `QueryDomainProfile` 注入允许表、资源路径、展示名称和禁止在对齐结果中泄漏的数据库标识符。

### 1.1 子图包契约

每个子图使用同一种目录边界：

```text
<subgraph>/
├── prompt/
│   ├── __init__.py  # Prompt 的公开入口
│   └── prompt.py    # 固定提示词、上下文构建器或无模型声明
├── node.py          # LangGraph 状态、节点和子图运行类
├── tool.py          # 该子图专属或聚合使用的工具协议
└── __init__.py      # 装配并公开稳定的子图入口
```

调用方必须从子图包的 `__init__.py` 导入子图运行类，不得从 `node.py` 拼装节点，也不得跨子图直接复用私有 Prompt。`pipeline.py` 通过公开入口编排对齐、Planning、翻译和审计；Planning 再通过自身工具调用 SQL 与塑形子图的公开入口。`prompt/` 可以按上下文职责继续拆分其他 Python 文件，但对外统一经由其 `__init__.py` 暴露。

`tool.py` 是子图工具的公开边界。业务对齐、SQL、新原料塑形、翻译和审计的终止工具由各自 `tool.py` 定义；规划子图的 `ask_user`、`think`、计划、表结构和单表检查协议位于自身 `tools/`，再由 `tool.py` 聚合。表结构读取与进程缓存归规划工具所有，下游只复用该公开能力；动态列文本契约归塑形子图所有。跨子图复用的 Pydantic Schema 生成、嵌套 JSON 参数兼容和参数错误反馈只是 Function Calling 协议算法，位于 `text2sql/function_calling/`，不被定义成共享业务工具。历史结构化塑形兼容分支仍不调用模型。

旧的 `app/agent/engine/`、`app/agent/runtime/`、`app/agent/tools/` 和 `app/agent/domains/` 已移除，不再提供兼容导入。应用代码、测试和实验脚本必须使用 `app.agent.text2sql` 下的公开入口，避免重新形成并行目录结构。

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
| `table-context/*.txt` | 按依赖顺序描述表用途、行粒度、`RELATIONSHIPS` 关系和查询不变量，供查询规划层选择关联路径 |
| `entity-lookup.json` | 可命名实体的 ID、展示字段、匹配模式和相似度预算 |
| `profile.py` | 业务域名称、范围、表白名单、资源顺序和友好表名 |

### 2.1 业务对齐层 `table-overview.txt` 表概述契约

`business-alignment/table-overview.txt` 是**业务对齐阶段**理解数据范围的轻量知识文件。它回答“系统中有哪些业务对象、各自代表什么、数据有什么会影响用户词汇理解的特性”，不回答“字段如何关联、SQL 应该如何查询”。

这里的“表概述”仅指业务对齐层文件，不能与查询规划层的 `table-context/*.txt` 混称。规划层表上下文会额外提供行粒度、查询口径和 `RELATIONSHIPS`；规划模型正是根据这些关系描述选择需要连接的表，再通过 `get_table_schema` 获取真实字段和外键。

文件使用 UTF-8 文本。空行分隔多条表记录；每条记录固定使用以下三个大写标签。加载时标签会转换为小写 YAML 键，并作为 `tables` 输入提供给业务对齐模型。

| 标签 | 是否必填 | 编写要求 | 对齐阶段用途 |
| --- | --- | --- | --- |
| `TABLE` | 是 | 表的稳定原始名称，必须与该业务域允许查询的表一致，例如 `proof_record`。不使用中文别名或临时名称。 | 让模型知道业务对象的稳定身份；对齐结果本身仍不得输出表名。 |
| `PURPOSE` | 是 | 一到两句说明该业务对象存储或表达什么，以及它帮助理解哪些用户概念。使用业务语言，不描述字段实现。 | 将“打卡”“积分余额”“商品兑换”等用户表达对应到正确业务对象。 |
| `DATA_CHARACTER` | 是 | 一到两句说明影响概念理解的数据特性，例如主数据或事件明细、当前快照或历史记录、可覆盖、定时刷新、跨赛季或仅结算期有效。 | 防止模型把快照当历史、把可覆盖记录当不可变流水，或把临时资格误解为长期数据。 |

一个业务域的表概述应覆盖其 `allowed_tables` 中的每张表，每张表恰好一条。记录顺序应按概念阅读顺序组织；业务对齐阶段不依赖表关联顺序。

**示例：**

```text
TABLE: proof_record
PURPOSE: 用户提交的运动凭证及审核过程，用于理解打卡、凭证、初审和终审等概念。
DATA_CHARACTER: 可被覆盖的运动事件与审核明细。
```

上述记录会被转换为以下稳定 YAML，而不是把原始 Markdown 或数据库字段说明直接交给模型：

```yaml
tables:
  - table: proof_record
    purpose: 用户提交的运动凭证及审核过程，用于理解打卡、凭证、初审和终审等概念。
    data_character: 可被覆盖的运动事件与审核明细。
```

表概述中**不得**写入字段名、外键、SQL、状态数值、连接条件或完整业务判定公式，也不得写表关系。这些内容会让业务对齐层过早依赖数据库实现，并与下一阶段职责重叠。用户词汇映射写入 `business-vocabulary.txt`；跨表关系、行粒度和默认查询口径写入 `table-context/*.txt`；需要精确执行的业务规则写入 `core-game-rules.txt` 或业务域计划校验器。

| 知识文件 | 面向阶段 | 回答的问题 | 不应包含 |
| --- | --- | --- | --- |
| `table-overview.txt` | 业务对齐 | 有哪些业务对象，数据有什么关键特性？ | 字段、表关系、SQL、精确筛选条件 |
| `business-vocabulary.txt` | 业务对齐 | 用户词汇对应哪个标准业务概念？ | 表实现和查询策略 |
| `table-context/*.txt` | 查询规划 | 一行代表什么、何时使用该表、与哪些表直接关联、默认口径是什么？ | 完整字段清单 |
| `core-game-rules.txt` | 对齐与规划 | 哪些业务事实、推论和例外必须遵守？ | 数据库字段实现细节 |
| 运行时表结构读取 | 查询规划与 SQL 生成 | 真实字段、类型、外键和字段备注是什么？ | 业务规则外推 |

新增业务域、将已有表加入业务域白名单，或改变表的可覆盖性、快照性质、生命周期范围等数据特性时，必须同步维护对应的表概述。仅修改字段、索引或字段备注时，不应为了同步而重复抄写到表概述；字段事实仍以 `description/db/` 与运行时表结构读取为准。

Profile 加载时会检查允许表非空且不重复、表标签完整、每张允许表对应一个表上下文文件、资源文件存在，以及受保护数据库标识符非空；`table-overview.txt` 的“三标签、每表一条”属于当前配置契约，解析器会将其转换为 YAML，但目前不会在启动时逐项校验标签内容。HTTP 请求只能选择显式注册表中的 `domain_key`，不能提供路径或动态导入位置。

新增积分等业务域时，应复制资源结构、按真实业务重新编写全部知识文件、建立独立 Profile，并在 `app/agent/text2sql/domains/registry.py` 显式注册。通用引擎、工具协议、会话和 SSE 不应随业务域复制。

当前已注册的 `rewards` 业务域覆盖用户、部门、赛季参与、赛季结算积分、积分流水、商品和奖品履约。它与 `sports` 使用相同的查询流水线，但拥有独立的表白名单、业务词汇、核心规则、实体匹配配置和查询计划校验器。业务域专属 Prompt 规则通过 Profile 注入，通用 Prompt 不再硬编码运动凭证语义。

---

## 3. 模型调用约束

模型统一通过 OpenAI 兼容 SDK 调用。`AGENT_QUERY_MODEL_PROVIDER` 为整条查询流水线选择唯一供应商，所有子图必须共享对应的地址、密钥、模型和超时，禁止形成阶段间混用。业务对齐、查询规划、单表候选检索、SQL 生成、结果翻译和结果审计的工具用途与参数均由 Pydantic 类说明及 `Field.description` 生成标准 Function Calling JSON Schema，不启用供应商 strict 模式，模型返回参数继续由相同 Pydantic 模型在本地校验。全部阶段按供应商协议关闭隐藏思考，并分别设置 `max_tokens`。生成轮次和工具次数另由 `AGENT_QUERY_*` 配置限制。

业务对齐层、查询规划层、单表候选检索、SQL 子图、新原料塑形、结果翻译和结果审计使用 `AsyncOpenAI`、异步 LangGraph 节点和 `ainvoke`，公开 `run` 返回可等待结果。由 `from_settings` 创建的子图拥有对应 SDK 客户端，并在运行成功、失败或取消后关闭客户端连接池；调用方自行注入的客户端默认仍由调用方管理。表结构缓存未命中时通过工作线程桥接现有同步读取器，候选数据和最终 SQL 则直接使用异步 MySQL 驱动。`AgentQueryPipeline.run` 直接等待上述异步子图；历史结构化塑形兼容分支继续通过 `asyncio.to_thread` 隔离同步计算。

### 3.1 全局模型供应商与工具标签模板

`AGENT_QUERY_MODEL_PROVIDER` 选择整条查询流水线使用的模型供应商：

| 值 | 连接配置 | 工具阶段地址 | 关闭思考的请求参数 |
| --- | --- | --- | --- |
| `deepseek` | `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_HTTP_TIMEOUT_SECONDS` | 所有模型阶段统一使用标准地址 | `extra_body.thinking.type=disabled` |
| `vllm` | `VLLM_API_KEY`、`VLLM_BASE_URL`、`VLLM_MODEL`、`VLLM_HTTP_TIMEOUT_SECONDS` | 始终使用配置的标准 OpenAI 兼容地址 | `extra_body.chat_template_kwargs.enable_thinking=false` |

供应商选择同时决定连接信息和关闭思考的请求参数，但不自动决定模型的工具标签格式。vLLM 可以承载 DeepSeek、Qwen、Kimi 等不同模型，其工具解析器和 Chat Template 必须在服务端正确配置，并与 `VLLM_MODEL` 公开的模型标识一致。新增供应商时，在 `shared/model_options.py` 增加连接解析和独立 `ModelRequestProfile`，不得在各子图内重复判断供应商。

`AGENT_QUERY_TOOL_TAG_TEMPLATE` 可以填写 `data/tool-tag/` 下的单个 `.txt` 文件名，是整条查询流水线的唯一模板配置源；业务对齐、查询规划、单表候选检索、SQL 生成、结果塑形、结果翻译和结果审计均复用该变量，不提供阶段专用覆盖配置。默认使用为 `deepseek-v4-flash` 提供的 `deepseek-v4.txt`，通过 vLLM 承载 Qwen3.6 时应改为 `qwen3.6.txt`，留空时不注入模板。模板只描述供应商标签语法，使用 `TOOL_NAME`、`PARAMETER_NAME` 和 `PARAMETER_VALUE` 等显式占位符，不得写入 `think`、`submit_sql_query` 等具体工具或任何业务内容；真实工具名和参数只以当轮 Function Calling Schema 为准。Qwen3.6 模板依据其[官方 `tokenizer_config.json`](https://huggingface.co/Qwen/Qwen3.6-27B/blob/main/tokenizer_config.json)使用 `<tool_call>`、`<function=...>` 和 `<parameter=...>` 标签，不得替换成 DeepSeek DSML 或早期 Qwen3 Hermes JSON 格式。运行时拒绝目录分隔符、非 `.txt` 文件、缺失文件、空文件及超过 `16000` 字符的内容，避免环境变量形成任意文件读取或无界 Prompt 注入。Docker 镜像会复制该受控目录。

工具标签模板会作为稳定格式提醒追加在原始用户任务末尾。模型返回普通文本、没有形成 OpenAI `tool_calls` 时，同一模板还会随精确协议错误再次加入紧邻重试上下文，明确要求使用标签结构而非正文模拟工具调用。模型修正后，运行时移除无效响应和临时错误反馈，但保留原始任务中的常驻提醒；参数 Schema 错误仍使用精确字段反馈，不重复增加模板。模板必须与实际模型版本以及 vLLM 的 `--tool-call-parser`、`--chat-template` 配置一致，不能仅根据 `deepseek` 或 `vllm` 请求体系自动猜测。

| 阶段 | 主要输出 | 工具策略 |
| --- | --- | --- |
| 业务对齐 | 标准业务需求或放弃理由 | Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，Prompt 要求每轮唯一工具调用且首轮使用 `think` |
| 查询规划 | 最终塑形结果选择或放弃理由 | Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，程序校验首轮 `think`、查询与塑形工具参数、结果 ID 归属、最终提交和修复顺序 |
| 单表候选 | 一条单表 SELECT | Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，程序要求唯一 `execute_single_table_select` |
| SQL 生成 | 参数化 SQL 草稿 | 只接收二字段 `MaterialSqlQueryPlan`；Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，程序要求唯一 `submit_sql_query` 并按错误阶段清理上下文 |
| 结果塑形计划 | 透传或动态转列布局 | 只接收塑形指导、SQL 输出列与前五行；Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，程序要求唯一 `submit_material_shape_plan` |
| 翻译目标识别 | 待翻译结果列及直接来源 | Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，程序要求唯一 `submit_translation_targets` |
| 注释映射提取 | 单字段原始值与展示值映射 | Pydantic 标准工具 Schema；每字段独立使用 `tool_choice=auto`，程序要求唯一 `submit_translation_rules` |
| 结果审计 | 相关性、表格说明和统计摘要 | Pydantic 标准工具 Schema；API 使用 `tool_choice=auto`，程序要求唯一 `submit_query_result_audit` 并有限修复 |

所有工具参数先由 Pydantic 校验。Schema 违规和业务校验失败以带原 `tool_call_id` 的正常工具结果加入模型上下文，反馈会指出错误字段、错误原因和修复动作。查询规划工具失败后仍向模型提供完整工具集并保持 `tool_choice=auto`，不再由状态机强制下一轮必须重提原工具；模型可以先思考、补查结构或澄清事实，再提交修正调用。`think.reason` 最多 `350` 个字符，用于记录已确认事实、仍需判断的问题和倾向的下一步动作；存在新的判断点时可以连续思考，但不得重复已有结论。连续两次成功调用 `think` 后，运行时通过共享 `system_guidance` 消息向紧邻请求临时提示模型判断是否应采取实际动作；请求返回后立即删除该消息，任一协议合法的非 `think` 调用都会重置计数。查询规划终止工具不再使用独立的固定修复次数；每次可修复失败及其后续辅助步骤都继续占用统一的规划生成次数和工具调用次数，直到成功调用 `submit_final_query_result`、主动放弃或达到 `AGENT_QUERY_PLANNING_MAX_GENERATIONS`、`AGENT_QUERY_PLANNING_MAX_TOOL_CALLS`，避免部署配置允许继续生成时被隐藏的小上限提前终止。当前默认分别为 30 次模型生成和 60 次工具调用；后者包含并行表结构读取、思考、原料查询、塑形和终止工具重提，不能按付费模型生成次数理解。

业务对齐、规划、单表候选、SQL、塑形、翻译和审计工具都优先按原始 Pydantic Schema 严格校验。若参数因 OpenAI 兼容工具解析器二次序列化而校验失败，兼容层会递归遍历外层参数字典和数组，只尝试还原以 `{` 或 `[` 开头且确实可解析的嵌套 JSON 字符串，然后重新执行完整 Pydantic 校验。首次校验已经成功时不会进入兼容转换，因此内容恰好是合法 JSON 的正常字符串字段仍保持原值；非法 JSON、额外字段和嵌套类型错误也不会被兼容接受。

业务对齐层在 `tool_choice=auto` 下由程序校验工具顺序和唯一性：首个有效调用必须是唯一的 `think`，后续每轮也只能调用一个已注册工具。配置的工具标签会从首轮开始随任务常驻；未调用工具、首轮工具错误、同轮多工具或未知工具不会立即终止，而会作为可重试协议反馈进入紧邻的下一轮上下文，其中无 `tool_calls` 的反馈会再次强调工具标签格式。模型完成修正后，运行时会从后续模型上下文中同时移除无效 assistant 响应及其临时错误反馈，避免已解决错误持续影响工具选择；内部诊断轨迹仍保留错误和修正过程，便于排障审计。

SQL 生成在 `tool_choice=auto` 下由程序要求唯一 `submit_sql_query`。若供应商响应未形成唯一工具调用，或 `finish_reason = length` 表示输出达到单次上限，子图不会在第一次偶发协议错误时直接终止，而是把稳定错误码和唯一修复动作加入普通上下文，在 `AGENT_QUERY_SQL_MAX_GENERATIONS` 内要求模型重提完整、紧凑的工具调用。无调用 ID 的反馈会再次附加全局工具标签；已有调用 ID 的错误使用对应 `tool` 消息返回。该机制不扩大 `DEEPSEEK_QUERY_SQL_MAX_TOKENS` 单次输出上限；用尽生成次数后按实际错误码结束。

SQL 修复上下文按失败阶段维护。工具协议或 Pydantic 参数错误在新工具参数成功解析后清除；AST、安全、计划一致性或分页校验错误只有在新 SQL 通过完整静态校验后清除；可修复数据库错误保留到后续草稿重新进入执行。更晚阶段的错误不会被一次无关的协议修复降级，原始模型响应和诊断轨迹也不会因上下文压缩而删除。

翻译目标识别与单字段注释映射同样在 `tool_choice=auto` 下要求唯一的指定工具。普通文本、多工具、错误工具、输出截断、Pydantic 参数错误、字段血缘错误和注释外推都会获得分类错误码与单一修复动作；没有调用 ID 时再次提供全局工具标签模板。每个调用最多进行一次局部修复，修正后即结束当前独立节点，因此旧错误上下文不会传给其他翻译字段或后续子图。单字段映射通过异步信号量受控并发，固定系统提示、仅含占位符的通用 tool-tag 和工具 Schema 位于动态字段 YAML 之前，以提高相邻请求的前缀复用机会。真实工具名和参数只来自当轮 Schema，不会由模板注入其他阶段的工具概念。

结果审计也在 `tool_choice=auto` 下要求唯一 `submit_query_result_audit`。普通文本、多工具、错误工具、输出截断和参数 Schema 错误分别返回稳定错误码；参数错误使用原调用 ID 的 `tool` 结果，缺少工具时使用带通用 tool-tag 的协议反馈，临时 API 请求失败使用不包含内部连接信息的独立反馈。每类错误最多修复或重试一次，失败后仍保留程序生成的完整表格和统计事实。审计子图通过异步 SDK 和 `ainvoke` 运行，不重新执行 SQL，也不重新统计完整结果。

部分兼容接口会把本应为对象或数组的工具参数二次编码为 JSON 字符串。规划入口只在首次 Pydantic 校验失败后递归解开可验证的嵌套 JSON，随后仍执行完全相同的 Pydantic、工具参数和业务域校验；无效 JSON、错误嵌套类型和额外字段不会被容错接受。工具收到可修复失败后，错误结果继续保留在模型上下文中，模型可以根据反馈补充更详细、更精确的信息后重新选择合法动作。

SQL 层只消费 `query_material_data` 提交的 `guidance` 和 `required_tables`。Planning 必须在指导中明确主体资格、明细重关联范围和所需稳定标识；SQL 生成器可以选择 JOIN、CTE 或相关子查询实现，但不能扩大声明的基础表集合，也不能把资格条件推迟给塑形。

跨查询与塑形的展示契约由 Planning 在两次工具调用之间维护。运动域的逐条凭证结果必须在查询指导中要求 `proof_record.id AS proof_record_id` 和“凭证记录 ID”表头，并在塑形结果中保持可见；Planning 观察真实表头后才能决定继续塑形或修正查询。

---

## 4. 模型上下文 YAML 契约

除 function calling 协议外，模型读取的结构化业务事实统一使用由 PyYAML 安全渲染的合法 YAML。业务对齐资源、核心规则、规划表概述、表结构、单表候选页、SQL 生成输入、翻译目标与单字段注释输入，以及结果审计输入均使用固定小写键，中文、多行 SQL、空值、布尔值和列表不通过手工字符串拼接。

每个模型阶段必须在稳定系统提示词中提供对应 YAML 键的中文含义。例如：

- `tables.table` 表示真实表名，`row_grain` 表示一行的业务含义；
- `columns.field_name`、`data_type`、`foreign_key` 和 `comment` 分别表示字段名、数据库类型、外键目标和数据库字段注释；
- `query_material_data` 的 `guidance` 表示 SQL 必须落实的筛选、资格和必取原料，`required_tables` 是基础表精确范围；运行时将二者投影为 `MaterialSqlQueryPlan`，该类型禁止携带额外字段。塑形要求不进入 SQL 子图，而是由 `shape_material_data` 以独立 `shaping_guidance` 提交；
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

其中 `comment` 原样来自数据库结构，不由模型补写或改写。工具成功执行后回传给模型的事实同样使用 YAML；工具参数、工具 JSON Schema 和带原 `tool_call_id` 的失败反馈继续使用 JSON，这是供应商函数调用协议的一部分，不能改成 YAML。

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
2. 基础表集合与 `query_material_data.required_tables` 声明完全一致，且全部属于业务域白名单。
3. 禁止注释、分号、多语句、通配字段、锁、`SELECT INTO`、数据库限定名和高风险函数。
4. WHERE、HAVING、JOIN 和嵌套量词子查询条件中的筛选标量必须使用命名占位符；只有 `EXISTS` 投影的 `SELECT 1` 与规划允许的分页整数例外，参数集合必须精确匹配。
5. 计划中显式写出的真实 `table.field` 必须存在于对应结构，并被 SQL 实际引用；不存在的字段返回规划层修正，遗漏字段在 SQL 预算内重试。
6. 最外层输出列必须具有唯一、稳定的 `snake_case` 名称，并与 `result_columns` 的名称和顺序完全一致。
7. `LIMIT`、`OFFSET` 只能使用非负整数；只有原料计划明确要求前 N、最近 N 或其他结果上限时才允许生成，系统不会追加隐藏上限。

SQL 模型可以根据原料计划选择 JOIN、CTE、相关子查询、分组或聚合，静态校验不再恢复旧查询块图并逐 JOIN 比对。集合资格必须在 `guidance` 中说明“如何确定合格主体”以及是否重新关联明细；资格范围与最终明细范围不同的场景还必须保留最窄作用域稳定标识，防止只按全局用户 ID 重新关联到其他赛季数据。

静态校验或数据库执行发现可修复错误时，运行时会使用原 `submit_sql_query` 的 `tool_call_id` 把错误类型、准确原因和唯一修复动作作为正常工具结果追加到当前 SQL 消息上下文。后续生成因此保留本次查询的短期修复记忆，并在 SQL 生成预算内重试；校验通过后，命名参数才编译为 asyncmy 参数绑定，在 `START TRANSACTION READ ONLY` 中执行，超时为 `10` 秒，最终始终回滚并关闭连接。连接、权限和超时错误直接交由系统处理，不要求模型改写 SQL。

运行时会同时保留两份语义等价的已校验 SQL：一份将命名参数编译为 `%s` 并仅供 asyncmy 绑定执行；另一份保留 `:parameter_name` 并仅供后续 AST 来源分析。两者均来自同一份通过表白名单、只读、字段、参数和分页校验的 SQL 草稿，不会执行第二条查询。

由于规划可按用户的完整导出或准确统计需求把 `limit` 设为 `null`，结果规模仍可能较大。部署时必须使用只读低权限数据库账号，并结合实际数据量监控查询时间、内存和返回体大小。

---

## 7. 结果塑形、翻译与数据暴露

Planning 调用 `query_material_data` 成功后，运行时为完整 `SqlQuerySubgraphResult` 分配仅本轮有效的原料结果 ID 并保存在后台缓存；模型只看到结果 ID、完整行数、真实表头和前 `5` 行预览。Planning 随后可以用该 ID 调用 `shape_material_data`，由工具查出完整原料结果并运行 `MaterialResultShapingSubgraph`：

1. 节点 1 只读取 `shaping_guidance`、准确 SQL 输出列和前 `5` 行原始样本，通过 `submit_material_shape_plan` 把布局指导编译为 `passthrough` 或 `pivot`。它不读取 SQL、表结构、查询条件或完整结果。
2. 程序验证计划只能引用真实输出列，且不能表达筛选器、聚合函数、计算列或常量列。协议、Schema 或字段引用错误最多修复一次。
3. 节点 2 对完整 SQL 原始结果确定性执行布局；逐行透传不会改值，动态转列按稳定主体键分组并按原料排序。发生同组值冲突、动态列键冲突或明确列数溢出时，返回失败且不产生部分表格。

塑形成功后，运行时同样只向 Planning 返回本轮塑形结果 ID、来源原料结果 ID、完整行数、真实表头和前 `5` 行预览，完整 `ResultShapingSubgraphResult` 的结果行留在后台缓存。失败的查询或塑形调用只返回可修复的友好反馈，不分配结果 ID。Planning 通过 `submit_final_query_result` 提交已成功观察的塑形结果 ID 后，系统先让操作员复核最终行数与表头；确认后主 Pipeline 才取出对应完整 SQL 与塑形结果，直接运行独立 `ResultTranslationSubgraph`，修正意见则返回 Planning 继续迭代：

1. 节点 1 将塑形列来源与保留命名占位符的已校验 SQL 血缘组合，再根据无注释表结构和前 `5` 行最终样本识别状态、审核状态、类型、启停标记或布尔编码。ID、名称、日期、数值度量、聚合和已由 SQL 表达式转换的列不会进入翻译。
2. 节点 2 为每个目标字段分别构造只含一个 `comment` 的 YAML 上下文，以 `AGENT_QUERY_TRANSLATION_MAX_PARALLEL_FIELDS` 限制并发。不同字段共享固定系统前缀以利用前缀缓存，但彼此不能读取其他字段注释。
3. 节点 3 由本地程序把经过校验的映射应用到完整塑形结果。模型映射中的原始值和展示值都必须能在该字段原始 `comment` 中找到；未知值和非目标字段保持原样。

翻译层保留塑形前的 SQL 原始行和塑形后的原始值，并另外生成翻译行。模型、工具协议或结构读取失败时，子图返回失败状态和塑形行副本，Pipeline 继续生成表格，不因展示增强失败丢弃已经成功读取并整理的数据。空结果不调用翻译模型。

历史 `NaturalLanguageQueryPlan + ResultShapePlan` 的模型和确定性塑形器仅为旧结果解析及独立回归测试保留；生产 Pipeline 只接受新 Planning 已选中的 `final_result`，不会重新执行历史双计划。

程序根据塑形后的实际结果列生成表头和完整 JSON 安全行，依据完整结果计算：

- 返回行数及是否达到规划上限；
- 低基数字符串、布尔或状态字段的类别分布；
- 排除 ID 和类别编码后的业务数值最小值与最大值。

审计模型只接收原问题、对齐结果、Planning 最终选择的来源信息、程序统计和前 `5` 行结果样本。它不能重新统计样本或推断未展示行。HTTP 响应可以返回完整表格，因此前端仍需评估大结果的分页、下载或虚拟滚动策略。

---

## 8. SSE 与运行时边界

会话为每个订阅者创建独立 `asyncio.Queue`。异步业务对齐节点直接在 FastAPI 事件循环发布进度；其余同步子图通过线程安全回调向该事件循环投递。事件历史使用固定长度队列；`Last-Event-ID` 只能补发仍在历史窗口内的事件。心跳间隔由环境配置控制，响应设置 `X-Accel-Buffering: no`。

由于认证使用 Bearer Header，浏览器原生 `EventSource` 无法直接携带现有令牌。管理前端应使用支持流式读取的 `fetch` 客户端，或在未来建立同源安全 Cookie/一次性 SSE 票据后再使用 `EventSource`。禁止把长期 Bearer Token 放进 URL 查询参数。

活动会话、待回答交互、订阅者和 SSE 事件仍为单进程内存实现，适配现有单 Worker 部署。只有 Pipeline 成功、会话进入 `completed` 且形成审计展示结果时，查询管理器才把状态响应、友好轨迹响应和结果响应原子写入 SQLite。失败、放弃、取消、运行中状态、SQL、工具参数和模型原始消息均不落盘。

SQLite 默认位于 `data/query-history/query-history.sqlite3`，相对路径固定以项目根目录解析，不依赖进程启动目录；本地目录结构为同级的 `app/` 与 `data/`，不会在 `app/` 内再次生成 `data/`。数据库使用 WAL 模式、五秒忙等待和单进程异步写锁。状态、轨迹和结果分别存为 JSON 列；状态或轨迹接口不会读取完整结果列，只有结果接口才按需加载表格。首次磁盘读取后，相应字段进入最多 `300` 项的进程内 LRU 热缓存；默认缓存 `600` 秒，命中时刷新 LRU 顺序但不延长固定 TTL，历史自身过期时缓存也立即失效。内存会话过期或服务重启后，普通状态、轨迹和结果接口会回退读取成功历史；SSE、回答和取消接口不从文件恢复运行时对象。生产镜像以 `/workspace` 为项目根目录，源码包位于 `/workspace/app`；Compose 使用 `flame_manage_data` 卷挂载同级的 `/workspace/data`，SQLite 主文件、WAL 辅助文件和其他运行数据均随卷保留。

成功历史按 `AGENT_QUERY_HISTORY_RETENTION_DAYS` 保留，读取和列表时顺带清理过期记录。文件包含用户问题、交互轨迹和查询结果，应限制目录与文件权限并作为敏感业务数据管理。多 Worker 或多实例仍不受支持；需要横向扩展时应迁移到共享存储，并保持事件序号与回答幂等协议。

---

## 9. 配置、验证与安全要求

相关配置分为模型连接、各阶段单次输出预算、翻译字段并发、生成与工具次数、活动会话、事件历史、会话保留、SSE 心跳和诊断日志。完整变量见 [FastAPI 应用结构与基础连接](application-structure.md)。

自动化测试应替换模型与数据库，不依赖真实外部服务。受控联调只允许连接开发数据库并执行只读问题。常规日志和测试产物不得包含密钥、密码、未校验 SQL 草稿、SQL 参数值、结果行、完整个人数据或模型原始响应；仅显式启用的 `trace` 诊断日志可以按下述受控边界记录模型消息。

终态会话不会长期保存 SQL、工具参数、表结构、Planning 后台原料副本或模型原文；成功会话只保留结果接口需要的最终表格行、程序统计和审计说明。SQL 阶段失败时只保留 `failure_stage`、稳定错误码、重试归属和已用/最大生成次数，结果接口可据此区分输出截断、工具协议、静态校验、数据库执行或基础设施错误，而不会暴露查询文本与原始异常。

### 9.1 生产诊断日志

`AGENT_QUERY_DIAGNOSTIC_LOG_ENABLED=true` 时，查询管理器以单行 JSON 向 Uvicorn 标准输出写入 `agent_query_diagnostic` 日志。每条事件包含 `query_id`、业务域、阶段、状态和事件类型；终态还包含各模型阶段的生成次数、SQL 稳定错误码、重试归属、行数和耗时。日志与 SSE 使用同一进度出口，但不进入查询历史接口，也不改变前端协议。

`AGENT_QUERY_DIAGNOSTIC_LOG_LEVEL` 支持：

| 值 | 记录范围 |
| --- | --- |
| `basic` | 阶段变化、终态、生成与工具次数、稳定错误码、行数和耗时 |
| `detailed` | 在 `basic` 基础上增加涉及表、结果字段、已校验参数化 SQL 模板和外部错误类型 |
| `trace` | 在 `detailed` 基础上增加所有模型节点的真实请求消息与 `assistant` 响应 |

`basic` 和 `detailed` 都不记录用户问题正文、交互答案、筛选参数值、模型原始响应、工具原始参数、SQL 结果行、凭证备注、图片路径、密钥或 Token。只有通过静态校验并完成参数绑定的 SQL 模板可以进入 `detailed` 日志；模型草稿和数据库返回值始终禁止记录。

`trace` 是生产排障期间显式启用的诊断等级。查询管理器为单次查询创建一个 `ModelMessageTraceQueue`，业务对齐、Planning、单表检索、SQL、塑形、翻译和审计共享该队列。每次模型调用前，统一调用边界把实际发送的 `messages` 作为 `request` 入队；调用完成后把模型返回的 `assistant` 消息作为 `response` 入队；请求异常只追加不含异常正文的 `error`。工具成功结果、Schema 错误和协议反馈会在实际进入下一轮模型请求时自然出现在该次 `request.messages` 中，不需要业务节点另外记录执行轨迹。

队列使用查询内单调递增的 `message_sequence` 保存全局顺序，并用 `stage` 标识模型节点。生产日志事件统一为 `model_message_trace`，不再使用业务对齐专属格式或由各业务分支拼接诊断文案。请求消息可能包含业务文本、工具参数和受限数据预览，只会统一遮蔽可识别的 Bearer Token 与 `sk-` 密钥。因此该等级只能在受控环境短期启用，日志访问权限和保留时间应按敏感业务日志管理；`basic` 和 `detailed` 不会创建或保留该消息队列。

当前 Compose 使用 Docker `json-file` 驱动保存 `manage-backend` 标准输出，并限制单文件 `20 MiB`、最多保留 `5` 个文件。排障时按查询记录 ID 查看：

```bash
cd /home/ubuntu/flame-sport-pheno-deploy
docker compose logs --since 30m manage-backend \
  | rg 'agent_query_diagnostic.*<query_id>'
```

诊断开关默认关闭。生产临时启用后必须重启 `manage-backend`，问题复现并导出所需日志后应恢复为 `false`，避免持续产生额外日志量。需要模型消息轨迹时同时设置 `AGENT_QUERY_DIAGNOSTIC_LOG_LEVEL=trace`；只设置 `detailed` 不会输出模型消息。
