"""
SQLite 存储层 — 接口模板、任务日志、用户、配置 KV。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import bcrypt
import uuid6

from .time import fmt_local_ts, now_readable, now_ts
from .types import AgentInterface, TaskLog, TaskStatus, StreamEntry, User

_db_path: str = ""


def init_store(data_dir: str) -> None:
    """初始化数据库连接和表结构（同步，启动时执行）。"""
    global _db_path
    _db_path = str(Path(data_dir) / "app.db")

    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_interfaces (
              id          TEXT PRIMARY KEY,
              name        TEXT NOT NULL,
              path        TEXT NOT NULL UNIQUE,
              prompt      TEXT NOT NULL,
              created_at  TEXT,
              updated_at  TEXT,
              created_ts  INTEGER,
              updated_ts  INTEGER
            );

            CREATE TABLE IF NOT EXISTS task_logs (
              task_id       TEXT PRIMARY KEY,
              linkid        TEXT,
              path          TEXT,
              interface_id  TEXT,
              status        TEXT,
              request_body  TEXT,
              request_response TEXT,
              prompt        TEXT,
              sdk_output    TEXT,
              sdk_trace     TEXT,
              sdk_stream    TEXT,
              callback_body TEXT,
              callback_response TEXT,
              callback_url  TEXT,
              error_message TEXT,
              created_at    TEXT,
              finished_at   TEXT,
              created_ts    INTEGER,
              finished_ts   INTEGER
            );

            CREATE TABLE IF NOT EXISTS users (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              username        TEXT UNIQUE NOT NULL,
              password_hash   TEXT NOT NULL,
              role            INTEGER DEFAULT 0,
              note            TEXT DEFAULT '',
              must_change_pwd INTEGER DEFAULT 0,
              user_version    INTEGER DEFAULT 1,
              created_at      TEXT,
              created_ts      INTEGER
            );

            CREATE TABLE IF NOT EXISTS config (
              key   TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              note  TEXT
            );
        """)

        # Migrations: add missing columns
        _ensure_columns(conn)

        # Backfill ts columns from old ISO timestamps
        _backfill_ts_columns(conn)

        _seed_defaults(conn)
        conn.commit()


def _connect() -> sqlite3.Connection:
    """创建同步连接。"""
    conn = sqlite3.connect(_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """确保所有迁移列存在。"""
    migrations = [
        # task_logs additions
        ("task_logs", "sdk_trace", "TEXT"),
        ("task_logs", "sdk_stream", "TEXT"),
        ("task_logs", "request_response", "TEXT"),
        ("task_logs", "callback_response", "TEXT"),
        ("task_logs", "created_ts", "INTEGER"),
        ("task_logs", "finished_ts", "INTEGER"),
        # users additions
        ("users", "must_change_pwd", "INTEGER DEFAULT 0"),
        ("users", "user_version", "INTEGER DEFAULT 1"),
        ("users", "created_ts", "INTEGER"),
        # agent_interfaces additions
        ("agent_interfaces", "created_ts", "INTEGER"),
        ("agent_interfaces", "updated_ts", "INTEGER"),
    ]
    for table, col, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # already exists


def _backfill_ts_columns(conn: sqlite3.Connection) -> None:
    """
    回填 *_ts 列：旧数据 *_at 如果是 ISO 字符串，转成 Unix 毫秒；
    同时把 *_at 从 ISO 转成可读本地时间（YYYY-MM-DD HH:MM:ss.SSS）。
    """
    from datetime import datetime

    rows = [
        ("agent_interfaces", "created_ts", "created_at"),
        ("agent_interfaces", "updated_ts", "updated_at"),
        ("task_logs", "created_ts", "created_at"),
        ("task_logs", "finished_ts", "finished_at"),
        ("users", "created_ts", "created_at"),
    ]

    for table, ts_col, at_col in rows:
        stale = conn.execute(
            f"SELECT rowid, {at_col} AS at_val FROM {table} WHERE {ts_col} IS NULL AND {at_col} IS NOT NULL"
        ).fetchall()
        if not stale:
            continue
        for r in stale:
            at_val = r["at_val"]
            try:
                # Try parsing as ISO (legacy)
                dt = datetime.fromisoformat(at_val.replace("Z", "+00:00"))
                ms = int(dt.timestamp() * 1000)
                readable = fmt_local_ts(ms)
                conn.execute(
                    f"UPDATE {table} SET {ts_col} = ?, {at_col} = ? WHERE rowid = ?",
                    (ms, readable, r["rowid"]),
                )
            except (ValueError, OSError):
                pass


# ============================================================
# 种子数据
# ============================================================

def _seed_defaults(conn: sqlite3.Connection) -> None:
    """初始化默认接口和 admin 用户。"""
    row = conn.execute("SELECT COUNT(*) AS c FROM agent_interfaces").fetchone()
    if row and row["c"] == 0:
        now = now_readable()
        ts = now_ts()
        inserts = [
            (uuid6.uuid7().hex, "收文分类", "doc/classify", _default_classify_prompt(), now, now, ts, ts),
            (uuid6.uuid7().hex, "流转目标识别", "doc/route", _default_route_prompt(), now, now, ts, ts),
            (uuid6.uuid7().hex, "发文审核", "doc/review", _default_dispatch_review_prompt(), now, now, ts, ts),
        ]
        conn.executemany(
            "INSERT INTO agent_interfaces (id, name, path, prompt, created_at, updated_at, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
            inserts,
        )

    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    if row and row["c"] == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, note, must_change_pwd, user_version, created_at, created_ts) VALUES (?,?,?,?,?,?,?,?)",
            ("admin", _hash_password("admin"), 1, "默认管理员", 1, 1, now_readable(), now_ts()),
        )


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def _check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ============================================================
# 默认提示词
# ============================================================

def _default_classify_prompt() -> str:
    return """# 文档处理任务

你需要处理一份电子公文。以下是请求信息：

```json
{{request.body}}
```

## 执行步骤

### 步骤 1：下载文件
遍历请求中的 `files` 数组，用 curl 下载每个 `file_url` 到工作目录。文件 URL 无需认证即可直接下载。

### 步骤 2：读取文件内容
用 Read 工具读取下载的文件。

### 步骤 3：分析并输出
根据文件内容和表单数据进行分析。

## 输出要求

请按以下 JSON Schema 输出最终结果，用 ```json 代码块包裹：

```json
{
  "need_human": false,
  "fail_reason": "",
  "doc_type": "company",
  "source_type": "group",
  "form_fill": {
    "file_no": "",
    "from_org": "",
    "deadline": "",
    "doc_date": ""
  },
  "opinion_text": "",
  "need_manual_restart": false,
  "ledger_data": null
}
```

## 注意事项
- 所有文本字段（fail_reason、opinion_text、desc 等）必须输出简体中文
- 文件下载前先检查本地是否已有该文件
- 如果文件无法下载，将 need_human 设为 true 并在 fail_reason 中说明原因
- 不要输出任何其他内容，只输出 JSON"""


def _default_route_prompt() -> str:
    return """# 流转目标识别任务

你需要根据审批意见识别下一流转节点。以下是请求信息：

```json
{{request.body}}
```

## 执行步骤

### 步骤 1：下载并读取文件
遍历请求中的 `files` 数组，用 curl 下载文件并用 Read 工具读取。

### 步骤 2：分析
结合 `approval_history`（审批意见）、`next_nodes`（可选节点）、`leader_division`（领导分工）等信息，判断应该选中的下一节点。

## 输出要求

请按以下 JSON Schema 输出最终结果，用 ```json 代码块包裹：

```json
{
  "need_human": false,
  "fail_reason": "",
  "next_node": {
    "id": "",
    "name": "",
    "type": "person"
  },
  "opinion_text": "",
  "form_fill": {},
  "remark": ""
}
```

## 注意事项
- 所有文本字段（fail_reason、opinion_text、remark 等）必须输出简体中文
- next_node 必须从 next_nodes / person_list / org_list 中选择已存在的节点
- 如果无法确定，将 need_human 设为 true 并在 fail_reason 中说明原因
- 不要输出任何其他内容，只输出 JSON"""


def _default_dispatch_review_prompt() -> str:
    return """# 发文审核任务

你需要对一篇发文进行全面的审核把关。以下是请求信息：

```json
{{request.body}}
```

## 请求结构说明

- `form_data`: 表单元数据（标题、文种、紧急程度、密级、主送机关等）
- `files`: 文件数组，其中 `file_type` = `"main_body"` 的是正文（.docx），其余为附件
- `approval_history`: 审批流转记录（含各部门会签意见）
- `workflowDefine_name`: 工作流名称
- `current_step`: 当前步骤

## 执行步骤

### 步骤 1：下载所有文件
- 遍历 `files` 数组，用 curl 下载每个 `file_url` 到工作目录
- 记录每个文件的原始文件名和类型（正文 / 附件）

### 步骤 2：提取正文文本
- 对正文（`file_type` = `"main_body"`）调用文档解析服务，获取纯文本
- 对附件也调用文档解析服务，用于步骤 5 中核对附件名称

### 步骤 3：提交 rdCLI 自动核稿（体例格式）
- 将正文 .docx 文件提交到 rdCLI 审稿服务（见 CLAUDE.md 中的调用说明）
- `additional_instructions` 设为 `"请严格按 GB/T 9704-2012 党政机关公文格式标准进行全面校对，重点关注：发文字号、标题层级、主送机关、正文结构、附件说明格式、落款（成文日期、印章）、专用语规范。仅输出有问题的项目，如果某类别无问题则无需列出。"`
- 通过轮询 `GET /api/proofread/task?id=<taskId>` 获取结果
- 每次轮询间隔 15 秒，最多等待 5 分钟
- 将 `data.result_text`（Markdown 核稿意见）保存下来，后续步骤会用到

### 步骤 4：Agent 独立审核（政策与合规 + 内容与逻辑）

结合正文文本和表单元数据，审核以下维度：

**（一）政策与合规审核**
- 发文是否必要：根据标题和正文内容判断，无实质内容则标记
- 文种选用是否正确：对照《党政机关公文处理工作条例》，检查文种（通知/报告/请示/函/纪要等）是否匹配正文内容和发文目的
- 引用文件是否有效：正文中引用的法律法规、上级文件等名称是否完整、文号是否正确
- 发文主体与落款是否一致

**（二）内容与逻辑审核**
- 是否"一文一事"：报告中不得夹带请示事项，请示中不得夹带报告事项
- 逻辑是否通顺：表述清楚、层次清晰、无歧义、无矛盾
- 关键要素是否完整：时间、地点、事由、办理要求等

**（三）程序与附件审核**
- 附件箱中文件名称是否与正文中"附件1""附件2"的顺序和名称一致
- 检查 `approval_history` 中是否完成了必要的部门会签（如果正文提到需要会签，但历史上没有对应的会签节点，则标记）
- 检查审批流程是否完整

### 步骤 5：汇总生成审核报告

将步骤 3（rdCLI 体例格式校对）和步骤 4（Agent 政策/内容/程序审核）的结果汇总为一份完整的 Markdown 审核报告。

## 输出要求

请按以下 JSON Schema 输出最终结果，用 ```json 代码块包裹：

```json
{
  "audit_result": "warning",
  "result_text": "## 发文审核报告\\n\\n**审核时间**: 2026-01-15\\n**标题**: xx公司关于xx的通知\\n\\n---\\n\\n### 一、政策与合规审核\\n\\n#### 1.1 发文必要性\\n该文涉及...确有必要发文。\\n\\n#### 1.2 文种选用\\n使用\\"通知\\"文种，符合...\\n\\n...\\n\\n### 二、内容与逻辑审核\\n\\n...\\n\\n### 三、体例格式审核\\n\\n（以下为 rdCLI 自动核稿意见）\\n\\n#### 3.1 错别字\\n...\\n\\n### 四、程序与附件审核\\n\\n...\\n\\n### 五、审核结论\\n\\n> 共发现 xx 个问题（严重 yy 项、一般 zz 项、提示 ww 项）。建议...",
  "issues_count": 5,
  "need_human": false
}
```

## 字段说明

| 字段 | 说明 |
|------|------|
| `audit_result` | `"pass"` 审核通过，可直接进入下一流程；`"warning"` 有问题但不卡流程，供拟稿人参考修改 |
| `result_text` | **完整的 Markdown 格式审核报告**，包含所有四个审核维度的详细意见，供前端直接渲染展示。rdCLI 的核稿意见整合到"体例格式审核"部分，原文/修改建议/修改理由逐条列出 |
| `issues_count` | 审核发现的问题总数（四项维度合计） |
| `need_human` | **永远设为 `false`**。审核结果无论通过或警告，都不需要人工介入，直接进入后续流程 |

## 注意事项
- 所有文本内容（`result_text`）必须使用简体中文
- `result_text` 中的 Markdown 要规范，使用 ## 作为一级标题、### 作为二级标题、**加粗**用于强调
- rdCLI 核稿意见原样保留到 `result_text` 的"体例格式审核"章节中，不要改写
- 如果某维度未发现问题，在报告中写"未发现问题"即可，不要留空
- 不要输出任何其他内容，只输出 JSON"""


# ============================================================
# 接口 CRUD
# ============================================================

class InterfacesStore:
    def list(self) -> list[AgentInterface]:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM agent_interfaces ORDER BY created_at DESC").fetchall()
        return [_row_to_interface(r) for r in rows]

    def get(self, id: str) -> AgentInterface | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM agent_interfaces WHERE id = ?", (id,)).fetchone()
        return _row_to_interface(row) if row else None

    def get_by_path(self, path: str) -> AgentInterface | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM agent_interfaces WHERE path = ?", (path,)).fetchone()
        return _row_to_interface(row) if row else None

    def create(self, name: str, path: str, prompt: str) -> AgentInterface:
        id = uuid6.uuid7().hex
        now = now_readable()
        ts = now_ts()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_interfaces (id, name, path, prompt, created_at, updated_at, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (id, name, path, prompt, now, now, ts, ts),
            )
            conn.commit()
        return self.get(id)  # type: ignore[return-value]

    def update(self, id: str, name: str, path: str, prompt: str) -> AgentInterface | None:
        now = now_readable()
        ts = now_ts()
        with _connect() as conn:
            conn.execute(
                "UPDATE agent_interfaces SET name=?, path=?, prompt=?, updated_at=?, updated_ts=? WHERE id=?",
                (name, path, prompt, now, ts, id),
            )
            conn.commit()
        return self.get(id)

    def remove(self, id: str) -> bool:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM agent_interfaces WHERE id = ?", (id,))
            conn.commit()
            return cur.rowcount > 0


# ============================================================
# 任务日志 CRUD
# ============================================================

class TaskLogsStore:
    def create(self, log: TaskLog) -> None:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO task_logs
                   (task_id, linkid, path, interface_id, status, request_body, request_response, prompt,
                    sdk_output, sdk_trace, sdk_stream, callback_body, callback_response, callback_url,
                    error_message, created_at, finished_at, created_ts, finished_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    log.task_id, log.linkid, log.path, log.interface_id,
                    log.status.value if isinstance(log.status, TaskStatus) else log.status,
                    log.request_body, log.request_response, log.prompt,
                    log.sdk_output, log.sdk_trace, log.sdk_stream,
                    log.callback_body, log.callback_response, log.callback_url,
                    log.error_message, log.created_at, log.finished_at,
                    log.created_ts, log.finished_ts,
                ),
            )
            conn.commit()

    def update_status(self, task_id: str, status: TaskStatus | str) -> None:
        s = status.value if isinstance(status, TaskStatus) else status
        with _connect() as conn:
            conn.execute("UPDATE task_logs SET status = ? WHERE task_id = ?", (s, task_id))
            conn.commit()

    def update_result(self, task_id: str, fields: dict[str, Any]) -> None:
        """更新任务日志的部分字段。finished_at 设置时自动附带 finished_ts。"""
        # Auto-add finished_ts when setting finished_at
        if "finished_at" in fields and "finished_ts" not in fields:
            fields = {**fields, "finished_ts": now_ts()}

        # Convert TaskStatus enum to string value
        if "status" in fields and isinstance(fields["status"], TaskStatus):
            fields["status"] = fields["status"].value

        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if v is None and k != "error_message":
                continue
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return
        vals.append(task_id)
        with _connect() as conn:
            conn.execute(f"UPDATE task_logs SET {', '.join(sets)} WHERE task_id = ?", vals)
            conn.commit()

    def get(self, task_id: str) -> TaskLog | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM task_logs WHERE task_id = ?", (task_id,)).fetchone()
        return _row_to_task_log(row) if row else None

    def get_latest_by_linkid(self, linkid: str) -> TaskLog | None:
        """按 linkid 取最新一条任务（linkid 不唯一，按创建时间倒序）。"""
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_logs WHERE linkid = ? ORDER BY created_ts DESC, rowid DESC LIMIT 1",
                (linkid,),
            ).fetchone()
        return _row_to_task_log(row) if row else None

    def list(self, limit: int = 100) -> list[TaskLog]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_logs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_task_log(r) for r in rows]

    def list_paged(self, offset: int, limit: int) -> tuple[list[TaskLog], int]:
        """分页列出任务日志（created_at DESC），返回 (items, total)。"""
        with _connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM task_logs").fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM task_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_task_log(r) for r in rows], total

    def count_active(self) -> int:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM task_logs WHERE status IN ('pending','processing')"
            ).fetchone()
        return row["c"] if row else 0

    def list_active(self) -> list[TaskLog]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_logs WHERE status IN ('pending','processing') ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_task_log(r) for r in rows]

    def append_stream(self, task_id: str, entries: list[StreamEntry]) -> None:
        """追加 SDK 流式输出条目到任务的 sdk_stream JSON 数组。"""
        if not entries:
            return
        with _connect() as conn:
            row = conn.execute(
                "SELECT sdk_stream FROM task_logs WHERE task_id = ?", (task_id,)
            ).fetchone()
            existing: list[dict] = _safe_parse_array(row["sdk_stream"]) if row and row["sdk_stream"] else []
            for e in entries:
                existing.append(_stream_entry_to_dict(e))
            conn.execute(
                "UPDATE task_logs SET sdk_stream = ? WHERE task_id = ?",
                (json.dumps(existing, ensure_ascii=False), task_id),
            )
            conn.commit()

    def get_stream(self, task_id: str) -> list[StreamEntry]:
        """读取任务当前已积累的 SDK 流式输出。"""
        with _connect() as conn:
            row = conn.execute(
                "SELECT sdk_stream FROM task_logs WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row and row["sdk_stream"]:
            return _safe_parse_array(row["sdk_stream"])
        return []

    def delete_expired(self, retention_days: int) -> int:
        """清理过期的非活跃任务记录（status 不在 pending/processing）。返回清理的任务数。"""
        cutoff_ms = now_ts() - retention_days * 24 * 3600 * 1000
        with _connect() as conn:
            cur = conn.execute(
                """DELETE FROM task_logs
                   WHERE created_ts < ?
                     AND status NOT IN ('pending', 'processing')""",
                (cutoff_ms,),
            )
            conn.commit()
            return cur.rowcount

    def list_task_ids(self) -> list[str]:
        """返回所有任务的 task_id 列表，用于清理对应的工作目录。"""
        with _connect() as conn:
            rows = conn.execute("SELECT task_id FROM task_logs").fetchall()
        return [r["task_id"] for r in rows]

    def search(self, task_id: str, linkid: str, limit: int = 100) -> list[TaskLog]:
        """按 task_id 或 linkid 查询任务（模糊匹配）。"""
        with _connect() as conn:
            if task_id and linkid:
                rows = conn.execute(
                    "SELECT * FROM task_logs WHERE task_id LIKE ? AND linkid LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{task_id}%", f"%{linkid}%", limit),
                ).fetchall()
            elif task_id:
                rows = conn.execute(
                    "SELECT * FROM task_logs WHERE task_id LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{task_id}%", limit),
                ).fetchall()
            elif linkid:
                rows = conn.execute(
                    "SELECT * FROM task_logs WHERE linkid LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (f"%{linkid}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_logs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_row_to_task_log(r) for r in rows]

    def search_paged(
        self, task_id: str, linkid: str, offset: int, limit: int
    ) -> tuple[list[TaskLog], int]:
        """分页搜索（模糊匹配），返回 (items, total)。"""
        like = lambda s: f"%{s}%"

        if task_id and linkid:
            like_params = [like(task_id), like(linkid)]
            with _connect() as conn:
                items = conn.execute(
                    "SELECT * FROM task_logs WHERE task_id LIKE ? AND linkid LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    like_params + [limit, offset],
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM task_logs WHERE task_id LIKE ? AND linkid LIKE ?",
                    like_params,
                ).fetchone()
            return [_row_to_task_log(r) for r in items], total["c"] if total else 0
        elif task_id:
            like_params = [like(task_id)]
            with _connect() as conn:
                items = conn.execute(
                    "SELECT * FROM task_logs WHERE task_id LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    like_params + [limit, offset],
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM task_logs WHERE task_id LIKE ?",
                    like_params,
                ).fetchone()
            return [_row_to_task_log(r) for r in items], total["c"] if total else 0
        elif linkid:
            like_params = [like(linkid)]
            with _connect() as conn:
                items = conn.execute(
                    "SELECT * FROM task_logs WHERE linkid LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    like_params + [limit, offset],
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) AS c FROM task_logs WHERE linkid LIKE ?",
                    like_params,
                ).fetchone()
            return [_row_to_task_log(r) for r in items], total["c"] if total else 0
        else:
            with _connect() as conn:
                total = conn.execute("SELECT COUNT(*) AS c FROM task_logs").fetchone()["c"]
                items = conn.execute(
                    "SELECT * FROM task_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [_row_to_task_log(r) for r in items], total


# ============================================================
# 用户 CRUD
# ============================================================

class UsersStore:
    def get(self, username: str) -> User | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, role, note, "
                "COALESCE(must_change_pwd,0) AS must_change_pwd, "
                "COALESCE(user_version,1) AS user_version, "
                "COALESCE(created_at,'') AS created_at, "
                "created_ts FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return _row_to_user(row) if row else None

    def list(self) -> list[User]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, username, password_hash, role, note, "
                "COALESCE(must_change_pwd,0) AS must_change_pwd, "
                "COALESCE(user_version,1) AS user_version, "
                "COALESCE(created_at,'') AS created_at, "
                "created_ts FROM users ORDER BY id"
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def update_password(self, username: str, hash: str) -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ?, user_version = user_version + 1 WHERE username = ?",
                (hash, username),
            )
            conn.commit()
            return cur.rowcount > 0

    def clear_must_change_pwd(self, username: str) -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE users SET must_change_pwd = 0 WHERE username = ?", (username,)
            )
            conn.commit()
            return cur.rowcount > 0

    def increment_user_version(self, username: str) -> None:
        """递增 user_version 以作废该用户所有旧 token（登出时使用）。"""
        with _connect() as conn:
            conn.execute(
                "UPDATE users SET user_version = user_version + 1 WHERE username = ?",
                (username,),
            )
            conn.commit()

    def get_all_versions(self) -> dict[str, int]:
        """获取 token 校验所需的全量版本快照: {username: user_version}。"""
        with _connect() as conn:
            rows = conn.execute(
                "SELECT username, COALESCE(user_version,1) AS user_version FROM users"
            ).fetchall()
        return {r["username"]: r["user_version"] for r in rows}

    def create(self, username: str, hash: str, role: int, note: str) -> bool:
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, note, must_change_pwd, user_version, created_at, created_ts) VALUES (?,?,?,?,?,?,?,?)",
                    (username, hash, role, note, 0, 1, now_readable(), now_ts()),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 用户名重复

    def remove(self, username: str) -> bool:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
            return cur.rowcount > 0


# ============================================================
# 配置 KV
# ============================================================

class ConfigStore:
    def get(self, key: str) -> str | None:
        with _connect() as conn:
            row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str, note: str = "") -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO config (key, value, note) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, note=excluded.note",
                (key, value, note),
            )
            conn.commit()


# ============================================================
# 导出单例
# ============================================================

interfaces_store = InterfacesStore()
task_logs_store = TaskLogsStore()
users_store = UsersStore()
config_store = ConfigStore()

# 公开密码工具函数
hash_password = _hash_password
compare_password = _check_password


# ============================================================
# 行转换 helper
# ============================================================

def _row_to_interface(row: sqlite3.Row) -> AgentInterface:
    return AgentInterface(
        id=row["id"],
        name=row["name"],
        path=row["path"],
        prompt=row["prompt"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
    )


def _row_to_task_log(row: sqlite3.Row) -> TaskLog:
    return TaskLog(
        task_id=row["task_id"],
        linkid=row["linkid"] or "",
        path=row["path"] or "",
        interface_id=row["interface_id"],
        status=TaskStatus(row["status"]),
        request_body=row["request_body"] or "",
        request_response=row["request_response"],
        prompt=row["prompt"] or "",
        sdk_output=row["sdk_output"],
        sdk_trace=row["sdk_trace"],
        sdk_stream=row["sdk_stream"],
        callback_body=row["callback_body"],
        callback_response=row["callback_response"],
        callback_url=row["callback_url"] or "",
        error_message=row["error_message"],
        created_at=row["created_at"] or "",
        finished_at=row["finished_at"],
        created_ts=row["created_ts"],
        finished_ts=row["finished_ts"],
    )


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        note=row["note"] or "",
        must_change_pwd=bool(row["must_change_pwd"]),
        user_version=row["user_version"],
        created_at=row["created_at"] or "",
        created_ts=row["created_ts"],
    )


def _safe_parse_array(s: str | None) -> list[StreamEntry]:
    """安全解析 JSON 数组为 StreamEntry 列表，失败时返回空。"""
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if not isinstance(parsed, list):
            return []
        result: list[StreamEntry] = []
        for item in parsed:
            if isinstance(item, dict):
                result.append(StreamEntry(
                    ts=item.get("ts", ""),
                    type=item.get("type", "text"),
                    text=item.get("text", ""),
                    tool_name=item.get("tool_name"),
                    tool_input=item.get("tool_input"),
                    tool_output=item.get("tool_output"),
                ))
        return result
    except (json.JSONDecodeError, TypeError):
        return []


def _stream_entry_to_dict(entry: StreamEntry) -> dict:
    d: dict[str, Any] = {"ts": entry.ts, "type": entry.type, "text": entry.text}
    if entry.tool_name:
        d["tool_name"] = entry.tool_name
    if entry.tool_input:
        d["tool_input"] = entry.tool_input
    if entry.tool_output:
        d["tool_output"] = entry.tool_output
    return d