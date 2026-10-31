# 公文流转 Session 复用设计方案

> 目标：电子公文在工作流中多轮流转时，同一公文的多次提交复用同一个 Claude Agent SDK session，
> 让模型能看到上一次的上下文（看过的文件、做过的判断、填过的字段），从而做出更连贯、更准确的决策。

---

## 1. 背景与现状

当前 `src/services/sdk_runner.py` 中，每次任务都独立调用 SDK：

```python
async for message in query(prompt=rendered_prompt, options=options):
    ...
```

其中 `options` 构造时：

```python
options = ClaudeAgentOptions(
    model=cfg.llm.model,
    cwd=str(work_dir),            # data/tasks/<taskId>/   ← 每次新建
    allowed_tools=[...],
    permission_mode="dontAsk",
    system_prompt=...,
)
```

没有传 `resume` 或 `session_id`，SDK 每次都自动创建全新 session，历史对话和已下载文件全部丢弃。

问题：同一公文（同一 `linkid`）在工作流中回流时（审批 → 打回 → 再提交），模型从零开始，
不知道上一次已经下载并解析过哪些文件、做过哪些判断，重复劳动且决策可能前后不一致。

---

## 2. 核心概念

| 概念 | 含义 | 唯一性 |
|------|------|--------|
| `linkid` | 业务方给的公文审批流水号，一份公文全程不变 | 一份公文一个 |
| `session_id` | Claude Agent SDK 的会话 id（UUID） | 一个 linkid 对应一个 |
| `task_id` | 我方每次收到一次提交生成的内部任务号 | 每次提交一个 |

三者关系：

```
linkid（一份公文）
  ├── session（一个持续对话，1:1）
  └── task_1, task_2, task_3 ...（每次提交一个，1:N）
```

---

## 3. 数据模型

只新增一张表。`task_logs` 表不变（task_id 仍每次新建，只用于记录单次提交的日志）。

### 3.1 `sessions` 表 —— linkid ↔ session_id 映射

```sql
CREATE TABLE IF NOT EXISTS sessions (
  session_id  TEXT PRIMARY KEY,          -- Claude Agent SDK 的 session id（UUID）
  linkid      TEXT NOT NULL UNIQUE,      -- 公文审批 id，一个公文一个 session
  status      TEXT NOT NULL DEFAULT 'active',  -- active | closed
  created_at  TEXT,
  updated_at  TEXT,
  created_ts  INTEGER,
  updated_ts  INTEGER
);
```

- 主键 `session_id`，`linkid` 加唯一约束（一个公文只有一个 session）。
- `status` 用于将来支持"公文办结后主动关闭 session"。

### 3.2 为什么不建 files 表

**结论：不建。** 文件内容已经存在于 session 的对话历史里，不需要我方额外缓存。

原因：
1. 一份公文牵扯到的文件是有限的，文件文本是有限的，总量不大。
2. 复用 session 后，模型上下文里天然带有上一次下载、调用 `doc_util_url` 解析服务提取出的文件文本。
   回流时模型直接"记得"，无需重新下载解析。
3. 若上下文确实找不到（例如被压缩、或历史过长被裁剪），只要在 CLAUDE.md 中告知模型：
   "如果上下文里没有某文件的内容，就自己 curl 下载、调用解析服务提取"，模型会自行兜底。

因此在 CLAUDE.md 的行为规范里加一条兜底指令即可，网关不做任何文件缓存。

---

## 4. 关键问题：session_id 的创建时序 vs 工作目录

这是本方案最容易踩坑的一点。

### 4.1 时序事实

- `session_id` 不是我们生成的，是 Claude Code SDK 底层进程在 `query()` **启动之后**生成的，
  并通过消息流返回（每条 SDK 消息都带 `session_id` 字段，首次可捕获）。
- `cwd`（工作目录）是 `query()` 的**输入参数**，必须在**调用之前**确定。

因此，"拿 SDK 自动生成的 session_id 当作工作目录名"存在先有鸡还是先有蛋的矛盾：
目录名要先定，session_id 后才知道。

### 4.2 两个可行方案

**方案 A：工作目录用 `linkid`（推荐）**

```
data/tasks/<linkid>/
```

- 首次提交：`cwd = data/tasks/<linkid>/`，不指定 `session_id`，SDK 自动生成；
  从返回消息里捕获 `session_id`，写入 `sessions` 表。
- 后续提交：查 `sessions` 表拿到 `session_id`，`query({ options: { resume: session_id, cwd: data/tasks/<linkid>/ } })`。

优点：
1. 目录名直观，运维时一看就知道是哪个公文。
2. 无时序矛盾（不依赖 session_id 做目录名）。
3. linkid 与 session 是 1:1，等价于"按公文隔离"。

**方案 B：工作目录用 `session_id`（可行，但需自己预生成）**

```
data/tasks/<session_id>/
```

- 利用 SDK 的 `options.session_id` 参数，我们**自己预先生成 UUID** 作为 session_id，
  同时用它做目录名，再传给 SDK：

  ```python
  session_id = uuid6.uuid7().hex               # 预生成
  work_dir = Path(data_dir) / "tasks" / session_id
  async for message in query(
      prompt=rendered_prompt,
      options=ClaudeAgentOptions(
          cwd=str(work_dir),
          session_id=session_id,
          ...,
      ),
  ):
      ...
  ```

- 后续提交直接 `resume: session_id`。

缺点：
1. 目录名是裸 UUID，不直观，排查困难（不知道对应哪个公文）。
2. 需要自己保证生成的 UUID 不与 SDK 内部冲突。

> **结论：推荐方案 A（目录用 linkid）。** 方案 B 技术上可行，但没有任何实际收益，反而牺牲了可读性。

```
答复：选择 方案A, 工作目录用 `linkid, data/tasks/<linkid>/
```

### 4.3 硬约束：resume 时 cwd 必须与原 session 一致

SDK 的 session 历史（transcript）存放在 `~/.claude/projects/<hash(cwd)>/<sessionId>.jsonl`，
按 `cwd` 定位。resume 时如果 `cwd` 变了，SDK 会找不到该 session 的历史。

因此**工作目录一旦创建就必须固定**。方案 A 中 `linkid` 全程不变，天然满足此约束。

---

## 5. Session 复用流程

### 5.1 整体流程

```
收到提交（含 linkid + files[]）
        │
        ▼
  用 linkid 查 sessions 表
        │
        ├── 命中 ──► 拿到 session_id，走「复用」分支
        │
        └── 未命中 ──► 走「新建」分支
```

### 5.2 新建 session（linkid 首次出现）

1. `cwd = data/tasks/<linkid>/`，`mkdir -p`。
2. 写 CLAUDE.md（行为规范 + 文件兜底指令，仅首次写入）。
3. 调用 `query()`，**不传 `resume` 也不传 `session_id`**，SDK 自动生成 session_id。
4. 从流式消息中捕获 `session_id`。
5. 处理结束后，写入 `sessions` 表：`(session_id, linkid, status='active')`。

### 5.3 复用 session（linkid 已存在）

1. 查 `sessions` 表，拿到 `session_id`。
2. `cwd = data/tasks/<linkid>/`（与首次一致，目录已存在）。
3. 调用 `query()`，传 `resume: session_id`。
   - `prompt` 仍正常传本次的渲染结果。SDK 会把本次 prompt 作为新的 user turn 追加到历史之后。
   - 模型看到全部历史 + 本次新数据，能"回忆"上次的文件内容和判断。
4. 处理结束后更新 `sessions.updated_at/updated_ts`。

### 5.4 文件内容的兜底

在 CLAUDE.md 行为规范中增加一条：

> 如果本次请求涉及某个文件，但当前上下文里没有该文件的文本内容（例如历史被压缩、或这是新文件），
> 按"文件下载"章节自行 curl 下载，并调用文档解析服务提取文本后再分析。
> 若上下文里已有该文件的文本，直接复用，无需重复下载。

这样既不需要 files 表，也覆盖了历史丢失的边界情况。

### 5.5 关键点：systemPrompt 与 CLAUDE.md 的处理

SDK 默认 `system_prompt` 是首次渲染后固化的，resume 时**不会**重新渲染。

影响：
- CLAUDE.md 里的服务地址、Token 说明等，只在首次 session 时生效。
- 若后续这些配置变了，已复用的 session 不会感知。

处理建议：
- 先做简单版：首次写入 CLAUDE.md 后不再变。
- 后续如有需要，可让 `sessions` 表记录 CLAUDE.md 内容 hash，版本不一致时关闭旧 session、新建一个。

---

## 6. 并发与边界情况

### 6.1 同一 linkid 并发提交

正常情况下工作流节点顺序流转，同一 linkid 不会并发。但可能因回调重试、上游重复投递等出现"几乎同时"到达。

**风险**：两个 `query()` 同时 `resume` 同一个 session_id，SDK 内部状态会冲突。

**对策**：内存中维护 `set[str]`（处理中 linkid 集合）：

- 提交到达时，若 linkid 已在处理中，则排队等待或返回 503（"该公文正在处理中"）。
- 处理完成/失败后释放标记。

> 此标记不需要持久化——服务重启后正在处理的 task 本就按现有 `_recover_orphaned_tasks` 逻辑处理。

### 6.2 服务重启

- `sessions` 表持久化在 SQLite，重启后仍在，resume 不受影响。
- 正在 processing 的任务按现有逻辑标记失败，不影响 session 复用（下次该 linkid 提交仍能 resume）。

### 6.3 清理策略

现有 `retention_days` 只清理 `task_logs` 和对应工作目录。需扩展：

- `task_logs` 删除后，`sessions` 可延迟到更长的保留期（或跟随公文办结状态）。
- 工作目录 `data/tasks/<linkid>/` 清理后，session 的对话历史仍在 `~/.claude/projects`，
  但工作目录里下载的文件会丢——但没关系，模型上下文里还有文件文本，且兜底指令会让它重新下载。
- SDK 侧的 session 历史如需主动清理，用 SDK 的 `delete_session(session_id)`。

### 6.4 对话膨胀

session 历史随回流次数增长，token 消耗增加。可选缓解：

- SDK `options.max_turns`：限制单次最多轮数。
- 定期 `delete_session` 重建。
- 先用简单版（不限制），观察实际膨胀速度再优化。

---

## 7. 改动清单

| 模块 | 改动 |
|------|------|
| `src/store.py` | 新增 `sessions` 表；新增 `sessions_store` CRUD；启动迁移 DDL |
| `src/types.py` | 新增 `Session` 类型；`SdkResult` 增加 `session_id` 字段 |
| `src/services/sdk_runner.py` | `run_agent` 增加 `resume_session_id` 参数；从消息流捕获并返回 `session_id`；工作目录改为 `data/tasks/<linkid>/`；CLAUDE.md 增加文件兜底指令 |
| `src/services/task_queue.py` | 提交前查 `sessions` 表决定新建/复用；处理完成后写 `sessions`；维护 `set[str]` 防并发 |
| `src/routes/process.py` | 透传 `linkid` 到队列（已有，无需改） |

### 待业务确认的问题

1. **session 关闭时机**：公文办结后是否主动关闭 session？由谁触发？
2. **保留期**：`sessions` 是否需要比 `task_logs` 更长的保留期？

```text
答复：就算公文办结了，session也保留，暂时按照保留30天来计算。每日清理一次超期的session。 我觉得公文一般30天基本就过去了，就算审批周期超过30天，大不了重新建session，那没办法。
```