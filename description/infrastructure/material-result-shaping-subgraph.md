# 原料结果塑形子图

## 1. 功能目标

原料结果塑形子图由 Planning 的 `shape_material_data` 工具调用，位于一次成功原料查询之后、字段翻译之前。工具根据 `material_result_id` 取得后台缓存的完整 SQL 原始结果，把本轮塑形指导编译为受约束布局，再由本地程序执行透传、分组和动态转列。

该子图只改变行列布局，不修改查询范围、主体资格和原始业务值。SQL 输入边界见[原料 SQL 查询子图](material-sql-query-subgraph.md)，完整应用顺序见[查询智能体应用编排](../application/query-agent.md)。

---

## 2. 输入与输出

模型动态上下文只包含：

| 输入 | 用途 |
| --- | --- |
| `shaping_guidance` | 确定输入行粒度、最终行粒度、普通字段、分组、排序和动态列 |
| `raw_material_headers` | SQL 原料表头；限制布局计划只能引用实际输出列，零行时仍完整提供 |
| `rows_preview` | 前五行原始样本，仅用于理解值形态 |

模型不读取 SQL、表结构、查询条件和完整结果。输出工具 `submit_material_shape_plan` 由 Pydantic 生成标准 Function Calling Schema，支持：

- `passthrough`：逐行复制指定可见列；
- `pivot`：按稳定主体键分组，按指定列稳定排序，把成员值展开为动态列。

最终结果包含列键、简洁表头、完整结果行、行数以及每个展示列对应的 SQL 来源列。`shape_material_data` 成功后才为它分配本轮唯一的 `shaped_result_id`，并在后台记录来源 `material_result_id`；规划模型只收到两个 ID、完整结果行数、真实表头和前 `5` 行 Markdown 预览，不接收完整结果行。来源元数据随最终选中结果交给后续字段翻译，用于组合真实 `table.field` 血缘。

---

## 3. 处理流程

```mermaid
flowchart LR
    A[塑形指导与受限样本] --> B[compile_shape_plan]
    B --> C[Schema 与字段引用校验]
    C -->|可修复| B
    C -->|通过| D[shape_rows]
    D --> E[带列来源的最终布局]
```

`compile_shape_plan` 使用 `tool_choice=auto`、非 strict Pydantic Schema、关闭隐藏思考和全局 tool-tag 模板。缺少工具、多工具、错误工具、参数错误和不存在的结果列分别返回准确错误；最多修复一次。程序还会解析塑形指导中的唯一动态列数量声明：固定 N 列时，模型必须把 `expected_pivot_columns` 精确编译为 N；由完整结果决定时必须为 `null`；不适用时必须使用 `passthrough`。不一致作为原工具失败结果反馈模型修正。

`shape_rows` 不调用模型。它始终处理完整 SQL 结果，而不是前五行样本。透传逐格复制原值；动态转列按稳定键分组并拒绝同组普通字段冲突。指导明确动态列数量时，即使完整结果为零行也先生成固定表头；实际成员超过该数量则失败，不能静默截断。

---

## 4. 边界与失败语义

塑形计划没有筛选器、表达式、聚合函数或常量列字段，因此模型不能通过工具参数改变业务事实。未列为可见、分组、排序或动态值的技术原料自然隐藏。

以下情况会使本次 `shape_material_data` 调用失败，且不返回部分表格或 `shaped_result_id`：

- 布局引用不存在的 SQL 输出列；
- 动态列键与普通列键冲突；
- 同一稳定主体组内的普通展示值不一致；
- 实际成员数量超过操作员确认的动态列数量；
- 工具协议或参数在有限修复后仍不合法。

失败结果会以友好原因和修正方向返回 Planning。原料完整时，Planning 可以继续引用同一个 `material_result_id` 并提交更精确的塑形指导；反馈指出原料缺失时，Planning 必须重新调用 `query_material_data`，不能用布局规则补造数据。

只有 Planning 通过 `submit_final_query_result` 选中成功的 `shaped_result_id` 后，主 Pipeline 才直接进入字段翻译。普通列和动态列都保留来源 SQL 别名，使翻译层能够读取对应数据库字段注释；翻译失败只降级为原始值，不回滚已完成的布局。

---

## 5. 配置与验证

`DEEPSEEK_QUERY_SHAPING_MAX_TOKENS` 限制单次布局计划输出，模型供应商、地址和工具标签沿用整条查询流水线的全局配置。

自动测试覆盖工具 Schema、受限 YAML 上下文、原料表头、`tool_choice=auto`、vLLM 关闭思考参数、动态转列、零行固定表头、固定列数修复、列来源保留、列数溢出、失败不产生结果 ID，以及 Planning 选中塑形结果后 Pipeline 直接进入翻译的执行顺序。
