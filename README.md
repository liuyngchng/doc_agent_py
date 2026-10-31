# dp_agt_py — 电子公文智能处理 Agent

基于 FastAPI + Claude Agent SDK (Python) 实现的电子公文智能处理网关。
功能涵盖：接口模板管理、提示词渲染、异步任务队列、Agent SDK 调用、回调投递与重试、管理后台。

## 功能清单

| 功能 | 实现位置 | 状态 |
|------|---------|------|
| 接收 JSON → 按 path 匹配模板 → 渲染 → 调用 Agent SDK → 回调 | `routes/process.py` | ✅ |
| 提示词占位符渲染 `{{request.xxx}}` | `services/prompt_engine.py` | ✅ |
| 异步任务队列 | `services/task_queue.py` | ✅ |
| Claude Agent SDK 封装 | `services/sdk_runner.py` | ✅ |
| 回调 + 指数退避重试 | `services/callback.py` | ✅ |
| SQLite 存储 | `store.py` (sqlite3) | ✅ |
| 接口 CRUD + 模拟操作 + 任务日志 | `routes/admin.py` | ✅ |
| 认证（登录/登出/改密/首次强制改密） | `routes/auth.py` | ✅ |
| 健康检查 | `routes/health.py` | ✅ |
| 任务查询/取消/追踪 | `routes/tasks.py` | ✅ |
| 管理后台前端 | `web/*` | ✅ |

## 技术栈

| 层 | 技术 |
|----|------|
| 运行时 | Python 3.11–3.12 |
| HTTP 框架 | FastAPI |
| Claude SDK | `claude-agent-sdk` |
| 存储 | sqlite3 (stdlib) |
| 日志 | logging (stdlib) |
| 密码 | bcrypt |
| UUID | uuid6 |
| HTTP 客户端 | httpx |
| 配置文件 | pyyaml |

## 快速开始

```bash
# 1. 安装依赖
cd /home/rd/workspace/dp_agt_py
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
dp_agt_py/
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
├── web/                       # 管理后台前端
├── data/                      # 运行时数据
│   ├── cfg.yml.template
│   ├── app.db
│   └── tasks/
└── build/
    ├── build.sh
    └── run.sh
```
