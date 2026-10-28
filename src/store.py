"""
SQLite 存储 — 对应 TypeScript 版 `store.ts`。

使用 aiosqlite 异步操作，兼容 FastAPI 的 async 生态。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import bcrypt
import uuid6
from .types import AgentInterface, TaskLog, TaskStatus, User

_db_path: str = ""


def init_store(data_dir: str) -> None:
    """初始化数据库连接和表结构（同步，启动时执行）。"""
    global _db_path
    db_path = str(Path(data_dir) / "app.db")
    _db_path = db_path

    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS agent_interfaces (
              id          TEXT PRIMARY KEY,
              name        TEXT NOT NULL,
              path        TEXT NOT NULL UNIQUE,
              prompt      TEXT NOT NULL,
              created_at  TEXT,
              updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS task_logs (
              task_id       TEXT PRIMARY KEY,
              linkid        TEXT,
              path          TEXT,
              interface_id  TEXT,
              status        TEXT,
              request_body  TEXT,
              prompt        TEXT,
              sdk_output    TEXT,
              sdk_trace     TEXT,
              callback_body TEXT,
              callback_url  TEXT,
              error_message TEXT,
              created_at    TEXT,
              finished_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              username        TEXT UNIQUE NOT NULL,
              password_hash   TEXT NOT NULL,
              role            INTEGER DEFAULT 0,
              note            TEXT DEFAULT '',
              must_change_pwd INTEGER DEFAULT 0,
              created_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS config (
              key   TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              note  TEXT
            );
        """)
        # Migration: add sdk_trace column if missing
        try:
            conn.execute("ALTER TABLE task_logs ADD COLUMN sdk_trace TEXT")
        except sqlite3.OperationalError:
            pass
        # Migration: add users/config tables
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                must_change_pwd INTEGER DEFAULT 0,
                created_at TEXT
            )""")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                note TEXT
            )""")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_pwd INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        _seed_defaults(conn)
        conn.commit()


def _connect() -> sqlite3.Connection:
    """创建同步连接（SQLite 在这个项目里写操作轻量，同步足够）。"""
    conn = sqlite3.connect(_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# 种子数据
# ============================================================

def _seed_defaults(conn: sqlite3.Connection) -> None:
    """初始化默认接口和 admin 用户。"""
    row = conn.execute("SELECT COUNT(*) AS c FROM agent_interfaces").fetchone()
    if row and row["c"] == 0:
        now = _now()
        inserts = [
            (uuid6.uuid7().hex, "收文分类", "document/classify", _default_classify_prompt(), now, now),
            (uuid6.uuid7().hex, "流转目标识别", "document/route", _default_route_prompt(), now, now),
            (uuid6.uuid7().hex, "发文审核", "document/dispatch-review", _default_dispatch_review_prompt(), now, now),
        ]
        conn.executemany(
            "INSERT INTO agent_interfaces (id, name, path, prompt, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            inserts,
        )

    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    if row and row["c"] == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, note, must_change_pwd, created_at) VALUES (?,?,?,?,?,?)",
            ("admin", _hash_password("admin"), 1, "默认管理员", 1, _now()),
        )


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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
- next_node 必须从 next_nodes / person_list / org_list 中选择已存在的节点
- 如果无法确定，将 need_human 设为 true 并在 fail_reason 中说明原因
- 不要输出任何其他内容，只输出 JSON"""


def _default_dispatch_review_prompt() -> str:
    return """# 发文审核任务

你需要对发文进行合规审核。以下是请求信息：

```json
{{request.body}}
```

## 执行步骤

### 步骤 1：下载并读取文件
遍历请求中的 `files` 数组，用 curl 下载文件并用 Read 工具读取。

### 步骤 2：审核
按照党政机关公文格式规范，对发文进行审核。

## 输出要求

请按以下 JSON Schema 输出最终结果，用 ```json 代码块包裹：

```json
{
  "audit_result": "warning",
  "issues": [
    {
      "category": "体例格式",
      "desc": "",
      "severity": "low",
      "location": ""
    }
  ],
  "suggestions": []
}
```

## 注意事项
- audit_result 取值为 pass / warning
- issues 中列出发现的问题
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
        now = _now()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_interfaces (id, name, path, prompt, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (id, name, path, prompt, now, now),
            )
            conn.commit()
        return self.get(id)  # type: ignore[return-value]

    def update(self, id: str, name: str, path: str, prompt: str) -> AgentInterface | None:
        now = _now()
        with _connect() as conn:
            conn.execute(
                "UPDATE agent_interfaces SET name=?, path=?, prompt=?, updated_at=? WHERE id=?",
                (name, path, prompt, now, id),
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
                   (task_id, linkid, path, interface_id, status, request_body, prompt,
                    sdk_output, sdk_trace, callback_body, callback_url, error_message,
                    created_at, finished_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    log.task_id, log.linkid, log.path, log.interface_id, log.status,
                    log.request_body, log.prompt, log.sdk_output, log.sdk_trace,
                    log.callback_body, log.callback_url, log.error_message,
                    log.created_at, log.finished_at,
                ),
            )
            conn.commit()

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        with _connect() as conn:
            conn.execute("UPDATE task_logs SET status = ? WHERE task_id = ?", (status, task_id))
            conn.commit()

    def update_result(self, task_id: str, fields: dict[str, Any]) -> None:
        """更新任务日志的部分字段。"""
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            if v is None and k not in ("error_message", "sdk_trace", "sdk_output",
                                        "callback_body", "finished_at"):
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

    def list(self, limit: int = 100) -> list[TaskLog]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_logs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_task_log(r) for r in rows]

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


# ============================================================
# 用户 CRUD
# ============================================================

class UsersStore:
    def get(self, username: str) -> User | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _row_to_user(row) if row else None

    def list(self) -> list[User]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, username, password_hash, role, note, "
                "COALESCE(must_change_pwd,0) AS must_change_pwd, "
                "COALESCE(created_at,'') AS created_at "
                "FROM users ORDER BY id"
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def update_password(self, username: str, hash: str) -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?", (hash, username)
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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_task_log(row: sqlite3.Row) -> TaskLog:
    return TaskLog(
        task_id=row["task_id"],
        linkid=row["linkid"] or "",
        path=row["path"] or "",
        interface_id=row["interface_id"],
        status=TaskStatus(row["status"]),
        request_body=row["request_body"] or "",
        prompt=row["prompt"] or "",
        sdk_output=row["sdk_output"],
        sdk_trace=row["sdk_trace"],
        callback_body=row["callback_body"],
        callback_url=row["callback_url"] or "",
        error_message=row["error_message"],
        created_at=row["created_at"] or "",
        finished_at=row["finished_at"],
    )


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        note=row["note"] or "",
        must_change_pwd=bool(row["must_change_pwd"]),
        created_at=row["created_at"] or "",
    )