# -*- coding: utf-8 -*-
"""
web.db —— SQLite 数据层（V2.2 §4.5）

WAL 模式，单写者（执行器），面板层只读或走队列。
不引 ORM，纯 sqlite3 标准库。
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "llmperf.db"
logger = logging.getLogger("llmperf.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_profiles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL UNIQUE,
  protocol      TEXT NOT NULL CHECK(protocol IN ('dify','openai_compat')),
  base_url      TEXT NOT NULL,
  api_key       TEXT,                   -- API Key（本地面板明文存储；web/data/ 已 gitignore，勿提交）
  model_name    TEXT,
  max_tokens    INTEGER DEFAULT 0,
  verify_ssl    INTEGER DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id    INTEGER REFERENCES config_profiles(id) ON DELETE SET NULL,
  test_type     TEXT NOT NULL CHECK(test_type IN ('baseline','concurrent','endurance')),
  script        TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','completed','failed','cancelled','orphaned')),
  params_json   TEXT NOT NULL,
  run_no        TEXT,                       -- 展示用日期 ID（YYYYMMDDHHMM，启动时刻），旧记录为 NULL
  started_at    TEXT, finished_at TEXT, duration_s REAL,
  summary_json  TEXT,
  result_dir    TEXT,
  html_report   TEXT,
  error         TEXT
);

CREATE TABLE IF NOT EXISTS corpora (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL UNIQUE,       -- 展示名/去重键（原始文件名，Web 优化 V2.0 §3.1）
  stored_name TEXT NOT NULL,              -- 落盘 uuid 化文件名（web/data/corpora/{stored_name}）
  size        INTEGER NOT NULL DEFAULT 0,
  line_count  INTEGER NOT NULL DEFAULT 0, -- 语料行数，供前端展示
  created_at  TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  test_run_id     INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
  analysis_run_id TEXT NOT NULL,
  engine          TEXT NOT NULL,
  findings_json   TEXT,
  report_md       TEXT,
  status          TEXT NOT NULL DEFAULT 'ok'
                  CHECK(status IN ('ok','failed')),
  created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON test_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created ON test_runs(id DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_run ON analyses(analysis_run_id);
"""

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """每线程一个连接（WAL 模式下读并发安全）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DB_PATH.parent, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    """建表（幂等）+ 老库增量迁移。"""
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    # 迁移：test_runs.run_no（日期展示 ID，Web 面板功能）——老库补列
    cols = {row[1] for row in conn.execute("PRAGMA table_info(test_runs)")}
    if "run_no" not in cols:
        conn.execute("ALTER TABLE test_runs ADD COLUMN run_no TEXT")
        logger.info("Migrated: test_runs.run_no added")
    # 迁移：config_profiles.api_key_enc → api_key（诚实命名：本地明文存储，从未实现加密）
    pcols = {row[1] for row in conn.execute("PRAGMA table_info(config_profiles)")}
    if "api_key" not in pcols and "api_key_enc" in pcols:
        conn.execute("ALTER TABLE config_profiles RENAME COLUMN api_key_enc TO api_key")
        logger.info("Migrated: config_profiles.api_key_enc renamed to api_key")
    conn.commit()


# ---- config_profiles CRUD ----

def list_profiles() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM config_profiles ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_profile(profile_id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM config_profiles WHERE id=?", (profile_id,)).fetchone()
    return dict(row) if row else None


def create_profile(name, protocol, base_url, api_key="", model_name="",
                   max_tokens=0, verify_ssl=1) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cur = _get_conn().execute(
        "INSERT INTO config_profiles (name,protocol,base_url,api_key,model_name,"
        "max_tokens,verify_ssl,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, protocol, base_url, api_key, model_name, max_tokens, verify_ssl, now, now),
    )
    _get_conn().commit()
    return cur.lastrowid


def update_profile(profile_id, **kwargs) -> bool:
    allowed = {"name", "protocol", "base_url", "api_key", "model_name",
               "max_tokens", "verify_ssl"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [profile_id]
    cur = _get_conn().execute(
        f"UPDATE config_profiles SET {set_clause} WHERE id=?", vals
    )
    _get_conn().commit()
    return cur.rowcount > 0


def delete_profile(profile_id) -> bool:
    cur = _get_conn().execute("DELETE FROM config_profiles WHERE id=?", (profile_id,))
    _get_conn().commit()
    return cur.rowcount > 0


# ---- test_runs CRUD ----

def create_test_run(profile_id, test_type, script, params: dict) -> int:
    cur = _get_conn().execute(
        "INSERT INTO test_runs (profile_id,test_type,script,status,params_json) "
        "VALUES (?,?,?,'pending',?)",
        (profile_id, test_type, script, json.dumps(params, ensure_ascii=False)),
    )
    _get_conn().commit()
    return cur.lastrowid


def update_test_run(run_id, **kwargs) -> bool:
    allowed = {"status", "run_no", "started_at", "finished_at", "duration_s",
               "summary_json", "result_dir", "html_report", "error"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [run_id]
    cur = _get_conn().execute(
        f"UPDATE test_runs SET {set_clause} WHERE id=?", vals
    )
    _get_conn().commit()
    return cur.rowcount > 0


def get_test_run(run_id) -> dict | None:
    row = _get_conn().execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_test_runs(limit=50) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM test_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_run_nos() -> set[str]:
    """全部已分配的日期 ID（run_no），供启动时去重（同分钟任务追加后缀）。"""
    rows = _get_conn().execute(
        "SELECT run_no FROM test_runs WHERE run_no IS NOT NULL"
    ).fetchall()
    return {r["run_no"] for r in rows}


def list_active_tasks() -> list[dict]:
    """执行中/待执行的任务（单队列执行器：同一时间只允许一个任务）。

    提交新任务前的并发防护查询；面板重启后 pending/running 均被
    mark_stale_tasks_orphaned 归位，不会永久占坑。
    """
    rows = _get_conn().execute(
        "SELECT id, run_no, script, status FROM test_runs "
        "WHERE status IN ('pending','running') ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_stale_tasks_orphaned():
    """面板启动时把残留 running/pending 任务改标 orphaned（V2.2 修订 #4）。

    running：子进程已随面板退出丢失（疑为孤儿，不自动杀进程以免误杀 CLI 手动运行）；
    pending：调度协程随面板退出丢失，重启后不可能再执行，一并归位，
    否则会永久阻塞 list_active_tasks 的并发防护（409）。
    """
    cur = _get_conn().execute(
        "UPDATE test_runs SET status='orphaned' WHERE status IN ('running','pending')"
    )
    _get_conn().commit()
    return cur.rowcount


def delete_test_run(run_id) -> bool:
    """删除任务记录（analyses 经外键 ON DELETE CASCADE 级联删除）。

    调用方负责先校验状态（running/pending 拒删）与清理磁盘产物。
    """
    cur = _get_conn().execute("DELETE FROM test_runs WHERE id=?", (run_id,))
    _get_conn().commit()
    return cur.rowcount > 0


# ---- corpora CRUD（Web 优化 V2.0 §3.1）----

def list_corpora() -> list[dict]:
    """全部上传语料（id 倒序）。"""
    rows = _get_conn().execute("SELECT * FROM corpora ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def get_corpus(corpus_id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM corpora WHERE id=?", (corpus_id,)).fetchone()
    return dict(row) if row else None


def get_corpus_by_name(name: str) -> dict | None:
    """按原始文件名查重（P1-5：同名上传 409 的预检，不依赖 UNIQUE 抛错）。"""
    row = _get_conn().execute("SELECT * FROM corpora WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def create_corpus(name: str, stored_name: str, size: int, line_count: int) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cur = _get_conn().execute(
        "INSERT INTO corpora (name,stored_name,size,line_count,created_at) VALUES (?,?,?,?,?)",
        (name, stored_name, size, line_count, now),
    )
    _get_conn().commit()
    return cur.lastrowid


def delete_corpus(corpus_id: int) -> bool:
    cur = _get_conn().execute("DELETE FROM corpora WHERE id=?", (corpus_id,))
    _get_conn().commit()
    return cur.rowcount > 0


def runs_referencing_corpus(stored_name: str) -> list[dict]:
    """找出 params_json 中引用过该语料落盘名的任务（P1-5：删除前引用检查）。

    任务提交时语料已解析为 web/data/corpora/{stored_name} 绝对路径存入 params_json，
    故按落盘名做模糊匹配即可覆盖引用。
    """
    rows = _get_conn().execute(
        "SELECT id, script, test_type, status FROM test_runs "
        "WHERE params_json LIKE ? ORDER BY id DESC",
        (f"%{stored_name}%",),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- analyses CRUD ----

def create_analysis(test_run_id, analysis_run_id, engine, findings_json=None,
                    report_md=None, status="ok") -> int:
    now = datetime.now().isoformat(timespec="seconds")
    cur = _get_conn().execute(
        "INSERT INTO analyses (test_run_id,analysis_run_id,engine,findings_json,"
        "report_md,status,created_at) VALUES (?,?,?,?,?,?,?)",
        (test_run_id, analysis_run_id, engine, findings_json, report_md, status, now),
    )
    _get_conn().commit()
    return cur.lastrowid


def get_analyses_by_run(analysis_run_id) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM analyses WHERE analysis_run_id=? ORDER BY id",
        (analysis_run_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_for_test(test_run_id) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM analyses WHERE test_run_id=? ORDER BY created_at DESC",
        (test_run_id,)
    ).fetchall()
    return [dict(r) for r in rows]
