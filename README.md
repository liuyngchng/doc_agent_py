# doc_agent_py — 电子公文智能处理 Agent（Python 版）

这是 doc_process_agent（TypeScript/Express 版）
的 Python 重写，功能等价，技术栈替换为 **FastAPI + Claude Agent SDK (Python)**。

## 与 TypeScript 版的功能一致性

| 功能 | TS 版 | Python 版 | 状态 |
|------|-------|-----------|------|
| 接收 JSON → 按 path 匹配模板 → 渲染 → 调用 Agent SDK → 回调 | `routes/process.ts` | `routes/process.py` | ✅ |
| 提示词占位符渲染 `{{request.xxx}}` | `services/prompt-engine.ts` | `services/prompt_engine.py` | ✅ |
| 异步任务队列 | `services/task-queue.ts` | `services/task_queue.py` | ✅ |
| Claude Agent SDK 封装 | `services/sdk-runner.ts` | `services/sdk_runner.py` | ✅ |
| 回调 + 指数退避重试 | `services/callback.ts` | `services/callback.py` | ✅ |
| SQLite 存储 | `store.ts` (better-sqlite3) | `store.py` (sqlite3) | ✅ |
| 接口 CRUD + 任务日志 | `routes/admin.ts` | `routes/admin.py` | ✅ |
| 认证（登录/登出/改密） | `routes/auth.ts` | `routes/auth.py` | ✅ |
| 健康检查 | `routes/health.ts` | `routes/health.py` | ✅ |
| 任务查询/取消/追踪 | `routes/tasks.ts` | `routes/tasks.py` | ✅ |
| 管理后台前端 | `web/*` | `web/*`（直接复用） | ✅ |

## 技术栈对照

| 层 | TS 版 | Python 版 |
|----|-------|-----------|
| 运行时 | Node.js ≥22 | Python 3.11–3.12 |
| HTTP 框架 | Express.js | FastAPI |
| Claude SDK | `@anthropic-ai/claude-agent-sdk` | `claude-agent-sdk` |
| 存储 | better-sqlite3 | sqlite3 (stdlib) |
| 日志 | pino | logging (stdlib) |
| 密码 | bcryptjs | bcrypt |
| UUID | uuid | uuid6 |
| HTTP 客户端 | node:http/https | httpx |
| 配置文件 | js-yaml | pyyaml |

## 关键差异说明

### 1. SDK 调用方式

TS 版使用 `query()` + `permissionMode: 'bypassPermissions'` + `tools: {preset: 'claude_code'}`：

```typescript
const q = query({ prompt, options: {
  cwd: workDir,
  permissionMode: 'bypassPermissions',
  tools: { type: 'preset', preset: 'claude_code' },
  model: cfg.llm.model,
  systemPrompt: { type: 'preset', preset: 'claude_code' },
}});
```

Python SDK 的等价写法是 `ClaudeAgentOptions` + 显式 `allowed_tools`：

```python
options = ClaudeAgentOptions(
    model=cfg.llm.model,
    cwd=str(work_dir),
    allowed_tools=["Bash", "Read", "Write", "Glob", "Grep", "WebSearch", "WebFetch"],
    permission_mode="bypassPermissions",  # 对应 permissionMode
    system_prompt=...,                     # 对应 systemPrompt
)
async for message in query(prompt=rendered_prompt, options=options):
    ...
```

> ⚠️ **注意**：Python SDK 的 `permission_mode` 取值是 `"default" | "acceptEdits" | "dontAsk"`，
> 没有 TS 版字面的 `"bypassPermissions"`。为保证"自动批准所有工具"的原语义，
> 这里用 `"dontAsk"`（不询问直接执行）替代。如果你的 SDK 版本支持其他取值，
> 请以 `claude_agent_sdk` 包内实际枚举为准。

### 2. 环境变量传递

TS 版 SDK 的 `env` 参数会整体替换子进程环境。Python SDK 无此参数的等价实现，
因此 `sdk_runner.py` 在每次 `run_agent()` 调用前通过 `os.environ.update()` 应用
`_build_env()` 返回的 LLM 配置覆盖项（所有任务共享同一 LLM，幂等覆盖无副作用）。

如果你的网关需要 `ANTHROPIC_BASE_URL` 等环境变量，也可以在启动
Python 进程前导出，由 `_build_env()` 从 `os.environ` 继承后再覆盖。

### 3. 异步模型

- TS 版用 Promise + 回调式 `pump()`
- Python 版用 `asyncio.create_task()` + `asyncio` 队列

两者都是"入队立即返回，Worker 异步消费"。

## 快速开始

```bash
# 1. 安装依赖
cd /home/rd/workspace/doc_agent_py
pip install -e .   # 或 python3 -m venv .venv && .venv/bin/pip install -e .

# 2. 准备配置
cp data/cfg.yml.template data/cfg.yml
# 编辑 data/cfg.yml，填写 llm.base_url（必填）

# 3. 启动（开发模式）
python -m src.index

# 4. 验证
curl http://localhost:20001/health
```

## Docker 部署

```bash
./build/build.sh      # 构建镜像
./build/run.sh        # 启动容器
```

## 目录结构

```
doc_agent_py/
├── pyproject.toml
├── Dockerfile
├── src/
│   ├── index.py               # FastAPI 入口
│   ├── config.py              # 配置加载 (cfg.yml → env)
│   ├── types.py               # 类型定义
│   ├── logger.py              # 日志
│   ├── store.py               # SQLite 存储 + CRUD
│   ├── routes/
│   │   ├── process.py         # POST /api/v1/{path}
│   │   ├── health.py          # GET /health
│   │   ├── tasks.py           # 任务查询/取消/追踪
│   │   ├── auth.py            # 登录/登出/改密
│   │   └── admin.py           # 管理后台 API
│   └── services/
│       ├── sdk_runner.py      # Claude Agent SDK 封装
│       ├── prompt_engine.py   # 提示词渲染
│       ├── callback.py        # 回调 + 重试
│       └── task_queue.py      # 任务队列
├── web/                       # 管理后台前端（复用 TS 版）
├── data/                      # 运行时数据
│   ├── cfg.yml.template
│   ├── app.db
│   └── tasks/
└── build/
    ├── build.sh
    └── run.sh
```
