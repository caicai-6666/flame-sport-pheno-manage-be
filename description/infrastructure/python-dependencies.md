# Python 运行依赖

> **文档目的**
>
> 本文档说明管理端后端的基础 Python 依赖、职责边界和环境安装方式。依赖的唯一安装入口是仓库根目录的 `requirements.txt`。

## 1. 环境基线

当前开发环境使用 Python 3.12，项目依赖通过 pip 安装：

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` 当前不锁定具体版本，由 pip 解析与当前 Python 环境兼容的版本。后续进入稳定交付阶段时，应根据部署与复现要求决定是否增加锁定文件；不能仅把某台开发机的完整 `pip freeze` 结果直接覆盖为项目依赖。

---

## 2. 依赖职责

| 依赖 | 主要用途 |
| --- | --- |
| `fastapi` | 构建管理端 HTTP API |
| `uvicorn[standard]` | 运行 ASGI 应用，并提供常用性能与开发辅助依赖 |
| `sqlmodel` | 定义数据模型并组织数据库访问 |
| `asyncmy` | 以异步方式连接 MySQL |
| `cryptography` | 提供认证、加密或签名场景需要的密码学能力 |
| `pydantic-settings` | 从环境变量和配置源加载应用设置 |
| `greenlet` | 支撑 SQLAlchemy 相关异步桥接能力 |
| `python-multipart` | 解析表单和文件上传请求 |
| `httpx` | 发起异步 HTTP 请求及编写接口测试 |
| `socksio` | 为配置 SOCKS 代理的 HTTP 客户端提供传输支持 |
| `openai` | 通过 OpenAI 兼容协议调用查询智能体使用的 DeepSeek 模型 |
| `langgraph` | 编排业务对齐、查询规划、SQL 查询、结果翻译和结果审计状态图 |
| `sqlglot` | 在候选读取和最终 SQL 执行前解析 MySQL AST，并执行静态安全校验 |
| `PyYAML` | 将查询智能体的业务上下文、表结构、查询计划和审计事实渲染为带固定字段语义的可读 YAML |
| `Pillow` | 校验和处理上传图片 |

> **注意**
>
> 查询智能体会在操作员创建查询后调用 DeepSeek，但不会执行凭证初审。认证、数据库会话、内部 HTTP、图片中转和查询运行时仍按各自功能文档保持独立边界；仅配置密钥不会自动发起模型请求。

---

## 3. 配置与安全

- 数据库密码、DeepSeek API Key 和其他凭证必须通过环境配置注入，禁止写入 `requirements.txt`、代码或文档。
- 外部 HTTP 和模型调用必须配置合理超时，重试应有上限并避免重复副作用。
- 图片上传必须校验大小、格式和内容，不能只相信扩展名或请求中的 MIME 类型。
- `cryptography` 只能使用经过验证的高层协议和项目既定方案，禁止自行设计加密算法。
- 依赖升级前应检查兼容性，并执行与变更风险相称的测试。

查询智能体的模型、SQL 与 SSE 安全约束见 [查询智能体运行时与业务域扩展](query-agent-runtime.md)。

---

## 4. 验证方式

安装完成后，至少执行以下导入检查：

```bash
python -c "import fastapi, uvicorn, sqlmodel, asyncmy, cryptography, pydantic_settings, greenlet, multipart, httpx, socksio, openai, langgraph, sqlglot, yaml, PIL"
```

还应检查 pip 能否解析出一致的依赖关系：

```bash
python -m pip check
```

两个命令均成功退出，表示当前解释器可以加载这些基础依赖，且 pip 未发现已安装包之间的声明式版本冲突。

---

## 5. 维护要求

新增、删除或替换运行依赖时，必须同步：

1. 更新根目录 `requirements.txt`。
2. 更新本文档中的用途和必要配置。
3. 更新 [项目文档地图](../README.md) 中的基础设施入口。
4. 验证安装、导入和依赖关系。
5. 在交付说明中列出新增依赖及引入原因。
