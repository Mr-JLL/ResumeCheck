"""
SQLite 数据库模块
================
跨岗位候选人库 + 评估记录 + 结果追踪。
所有岗位共用一个数据库，candidates 表去重，evaluations 表记录每次评估，
outcomes 表追踪 HR 实际操作（联系/面试/录用），用于学习机制。

WAL 模式启用，支持局域网多人同时读取 + 偶发写入。
"""

import os
import sys
import json
import sqlite3
import logging
import datetime
from contextlib import contextmanager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "resumes.db")


# =============================================================================
# Schema
# =============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    english_required TEXT,
    min_education TEXT,
    min_years INTEGER,
    industry_required TEXT,
    requires_management INTEGER DEFAULT 0,
    config_json TEXT,
    prompt_text TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT UNIQUE NOT NULL,
    name TEXT,
    age INTEGER,
    first_degree TEXT,
    school TEXT,
    major TEXT,
    english_level TEXT,
    total_years INTEGER,
    raw_html_path TEXT,
    structured_json TEXT,
    duplicate_of_id INTEGER,
    source TEXT DEFAULT 'scraped',
    ability_fingerprint TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    pros_json TEXT,
    cons_json TEXT,
    matches_json TEXT,
    mismatches_json TEXT,
    verdict_reason TEXT,
    has_hard_fail INTEGER DEFAULT 0,
    scrape_session_id TEXT,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(id),
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    UNIQUE(candidate_id, job_id)
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    action_at TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY(evaluation_id) REFERENCES evaluations(id)
);

CREATE TABLE IF NOT EXISTS rejection_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    tag_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(evaluation_id) REFERENCES evaluations(id),
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS prefilter_rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    fail_reason TEXT,
    name_hint TEXT,
    scrape_session_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(resume_id, job_id)
);

CREATE TABLE IF NOT EXISTS talent_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL UNIQUE,
    recontact_date TEXT NOT NULL,
    reason_tag TEXT DEFAULT '',
    note TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(evaluation_id) REFERENCES evaluations(id)
);

CREATE INDEX IF NOT EXISTS idx_eval_job ON evaluations(job_id);
CREATE INDEX IF NOT EXISTS idx_eval_verdict ON evaluations(verdict);
CREATE INDEX IF NOT EXISTS idx_eval_at ON evaluations(evaluated_at);
CREATE INDEX IF NOT EXISTS idx_outcome_action ON outcomes(action);
CREATE INDEX IF NOT EXISTS idx_outcome_eval ON outcomes(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_candidate_resume ON candidates(resume_id);
CREATE INDEX IF NOT EXISTS idx_tag_text ON rejection_tags(tag_text);
CREATE INDEX IF NOT EXISTS idx_tag_job ON rejection_tags(job_id);
CREATE INDEX IF NOT EXISTS idx_pf_resume ON prefilter_rejects(resume_id);
CREATE INDEX IF NOT EXISTS idx_pf_job ON prefilter_rejects(job_id);
CREATE INDEX IF NOT EXISTS idx_pool_date ON talent_pool(recontact_date);
CREATE INDEX IF NOT EXISTS idx_pool_status ON talent_pool(status);
CREATE INDEX IF NOT EXISTS idx_pool_eval ON talent_pool(evaluation_id);

CREATE TABLE IF NOT EXISTS hr_stage_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL UNIQUE,
    job_id INTEGER NOT NULL,
    stages_json TEXT DEFAULT '[]',
    reject_reason TEXT DEFAULT '',
    note TEXT DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(evaluation_id) REFERENCES evaluations(id),
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_hrstage_eval ON hr_stage_tags(evaluation_id);
CREATE INDEX IF NOT EXISTS idx_hrstage_job ON hr_stage_tags(job_id);

CREATE TABLE IF NOT EXISTS preference_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    ai_verdict TEXT,
    hr_action TEXT,
    dwell_seconds INTEGER,
    rejection_tag TEXT,
    candidate_snapshot TEXT,
    analyzed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preference_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    rule_text TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    evidence_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_psig_job ON preference_signals(job_id);
CREATE INDEX IF NOT EXISTS idx_psig_analyzed ON preference_signals(analyzed);
CREATE INDEX IF NOT EXISTS idx_prule_status ON preference_rules(status);
CREATE INDEX IF NOT EXISTS idx_prule_job ON preference_rules(job_id);

CREATE TABLE IF NOT EXISTS scrape_sessions (
    id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    device_name TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total_scraped INTEGER DEFAULT 0,
    passed_prefilter INTEGER DEFAULT 0,
    ai_excluded INTEGER DEFAULT 0,
    ai_passed INTEGER DEFAULT 0,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_ss_job ON scrape_sessions(job_id);
CREATE INDEX IF NOT EXISTS idx_ss_started ON scrape_sessions(started_at);

CREATE TABLE IF NOT EXISTS correction_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    eval_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    condition_name TEXT NOT NULL,
    error_type TEXT,
    evidence_text TEXT,
    hr_note TEXT,
    analyzed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(eval_id) REFERENCES evaluations(id)
);
CREATE INDEX IF NOT EXISTS idx_csig_job ON correction_signals(job_id);
CREATE INDEX IF NOT EXISTS idx_csig_analyzed ON correction_signals(analyzed);
CREATE INDEX IF NOT EXISTS idx_csig_cond ON correction_signals(condition_name);

CREATE TABLE IF NOT EXISTS job_criteria_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    condition_name TEXT NOT NULL,
    note_text TEXT NOT NULL,
    is_hard INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    source_signal_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    confirmed_at TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_jcn_job ON job_criteria_notes(job_id);
CREATE INDEX IF NOT EXISTS idx_jcn_status ON job_criteria_notes(status);
"""

# 列迁移后才能创建的索引
POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_candidate_dup ON candidates(duplicate_of_id)",
]


# 旧库迁移：为 evaluations / candidates 已存在的实例补列
MIGRATIONS = [
    ("evaluations", "matches_json", "TEXT"),
    ("evaluations", "mismatches_json", "TEXT"),
    ("evaluations", "verdict_reason", "TEXT"),
    ("candidates", "duplicate_of_id", "INTEGER"),
    ("evaluations", "scrape_session_id", "TEXT"),
    ("outcomes", "dwell_seconds", "INTEGER"),
    ("prefilter_rejects", "scrape_session_id", "TEXT"),
    ("candidates", "source", "TEXT DEFAULT 'scraped'"),
    ("jobs", "rule_config", "TEXT"),
    ("candidates", "ability_fingerprint", "TEXT"),
    ("jobs", "english_required", "TEXT DEFAULT 'no'"),
]


# =============================================================================
# Connection management
# =============================================================================

@contextmanager
def get_conn():
    """提供一个上下文管理的数据库连接（WAL 模式，支持并发读 + 单写）"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_exists(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def init_db():
    """初始化数据库（首次运行时调用）+ 兼容旧库迁移"""
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        # 增量列迁移（必须在创建依赖索引之前完成）
        for table, col, coltype in MIGRATIONS:
            if not _column_exists(conn, table, col):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                    logger.info(f"已迁移：{table}.{col}")
                except Exception as e:
                    logger.warning(f"迁移 {table}.{col} 失败：{e}")
        # 迁移完成后再创建依赖新列的索引
        for sql in POST_MIGRATION_INDEXES:
            try:
                conn.execute(sql)
            except Exception as e:
                logger.warning(f"创建后续索引失败：{e}")
        # 数据迁移：浅绿→蓝色（幂等，可重复执行）
        try:
            conn.execute("UPDATE evaluations SET verdict='蓝色' WHERE verdict='浅绿'")
        except Exception as e:
            logger.warning(f"数据迁移 浅绿→蓝色 失败：{e}")
    logger.info(f"✅ 数据库已初始化: {DB_PATH}")


def now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


# =============================================================================
# Jobs
# =============================================================================

def upsert_job(name, config_dict, prompt_text):
    """新增或更新岗位配置"""
    industry = config_dict.get("industry_required", "")
    if isinstance(industry, list):
        industry = "、".join(industry)
    with get_conn() as conn:
        cur = conn.cursor()
        existing = cur.execute("SELECT id FROM jobs WHERE name=?", (name,)).fetchone()
        params = {
            "name": name,
            "english_required": config_dict.get("english_required", "no"),
            "min_education": config_dict.get("min_education", "本科"),
            "min_years": config_dict.get("min_years", 0),
            "industry_required": industry,
            "requires_management": 1 if config_dict.get("requires_management") else 0,
            "config_json": json.dumps(config_dict, ensure_ascii=False),
            "prompt_text": prompt_text,
            "now": now_iso(),
        }
        if existing:
            cur.execute("""
                UPDATE jobs SET english_required=:english_required, min_education=:min_education,
                    min_years=:min_years, industry_required=:industry_required,
                    requires_management=:requires_management, config_json=:config_json,
                    prompt_text=:prompt_text, updated_at=:now
                WHERE name=:name
            """, params)
            return existing["id"]
        else:
            params["created"] = params["now"]
            cur.execute("""
                INSERT INTO jobs (name, english_required, min_education, min_years,
                    industry_required, requires_management, config_json, prompt_text,
                    created_at, updated_at)
                VALUES (:name, :english_required, :min_education, :min_years,
                    :industry_required, :requires_management, :config_json, :prompt_text,
                    :created, :now)
            """, params)
            return cur.lastrowid


def get_job(name):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def get_job_by_id(job_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def update_job_rule_config(job_id, rule_config_json):
    """更新岗位声明式规则配置。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET rule_config=?, updated_at=? WHERE id=?",
            (rule_config_json, now_iso(), job_id))


def update_job_eval_rules(job_name, rules_list):
    """更新 config_json.rules（AI 评判规则，含 evidence_hint）。"""
    job = get_job(job_name)
    if not job:
        return False
    try:
        config = json.loads(job.get("config_json") or "{}")
    except Exception:
        config = {}
    config["rules"] = rules_list
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET config_json=?, updated_at=? WHERE name=?",
            (json.dumps(config, ensure_ascii=False), now_iso(), job_name)
        )
    return True


def list_jobs():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def delete_job(job_id):
    """删除岗位（级联删除该岗位的所有评估、操作、标签）"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM outcomes WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE job_id=?)
        """, (job_id,))
        cur.execute("DELETE FROM rejection_tags WHERE job_id=?", (job_id,))
        cur.execute("DELETE FROM evaluations WHERE job_id=?", (job_id,))
        cur.execute("DELETE FROM prefilter_rejects WHERE job_id=?", (job_id,))
        cur.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return cur.rowcount


# =============================================================================
# Candidates
# =============================================================================

def upsert_candidate(resume_id, info):
    """
    info: dict containing name, age, first_degree, school, major,
          english_level, total_years, raw_html_path, structured_json (str or dict),
          optional duplicate_of_id
    """
    structured = info.get("structured_json")
    if isinstance(structured, dict):
        structured = json.dumps(structured, ensure_ascii=False)

    with get_conn() as conn:
        cur = conn.cursor()
        existing = cur.execute("SELECT id FROM candidates WHERE resume_id=?",
                               (resume_id,)).fetchone()
        params = {
            "resume_id": resume_id,
            "name": info.get("name"),
            "age": info.get("age"),
            "first_degree": info.get("first_degree"),
            "school": info.get("school"),
            "major": info.get("major"),
            "english_level": info.get("english_level"),
            "total_years": info.get("total_years"),
            "raw_html_path": info.get("raw_html_path"),
            "structured_json": structured,
            "duplicate_of_id": info.get("duplicate_of_id"),
            "source": info.get("source", "scraped"),
            "now": now_iso(),
        }
        if existing:
            cur.execute("""
                UPDATE candidates SET name=:name, age=:age, first_degree=:first_degree,
                    school=:school, major=:major, english_level=:english_level,
                    total_years=:total_years, raw_html_path=:raw_html_path,
                    structured_json=:structured_json, duplicate_of_id=:duplicate_of_id,
                    source=:source
                WHERE resume_id=:resume_id
            """, params)
            return existing["id"]
        else:
            cur.execute("""
                INSERT INTO candidates (resume_id, name, age, first_degree, school, major,
                    english_level, total_years, raw_html_path, structured_json,
                    duplicate_of_id, source, created_at)
                VALUES (:resume_id, :name, :age, :first_degree, :school, :major,
                    :english_level, :total_years, :raw_html_path, :structured_json,
                    :duplicate_of_id, :source, :now)
            """, params)
            return cur.lastrowid


def get_candidate_by_resume_id(resume_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE resume_id=?",
                           (resume_id,)).fetchone()
        return dict(row) if row else None


def get_candidate_by_id(candidate_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM candidates WHERE id=?",
                           (candidate_id,)).fetchone()
        return dict(row) if row else None


def list_all_candidates_min():
    """轻量列出所有候选人，用于 ghost detection 的相似匹配"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, resume_id, name, age, school, first_degree
            FROM candidates
            ORDER BY id
        """).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# Prefilter rejects（轻量记录，不存 structured_json）
# =============================================================================

def upsert_prefilter_reject(resume_id, job_id, fail_reason, name_hint=None, session_id=None):
    with get_conn() as conn:
        cur = conn.cursor()
        existing = cur.execute("""
            SELECT id FROM prefilter_rejects WHERE resume_id=? AND job_id=?
        """, (resume_id, job_id)).fetchone()
        if existing:
            cur.execute("""
                UPDATE prefilter_rejects SET fail_reason=?, name_hint=?, scrape_session_id=?, created_at=?
                WHERE id=?
            """, (fail_reason, name_hint, session_id, now_iso(), existing["id"]))
            return existing["id"]
        cur.execute("""
            INSERT INTO prefilter_rejects (resume_id, job_id, fail_reason, name_hint, scrape_session_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (resume_id, job_id, fail_reason, name_hint, session_id, now_iso()))
        return cur.lastrowid


def list_manual_imports_for_job(job_name):
    """查询某岗位的自投简历评估列表"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.*, c.resume_id, c.name, c.age, c.first_degree, c.school,
                   c.major, c.english_level, c.total_years, c.raw_html_path, c.source
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ? AND e.verdict = '自投'
        """, (job_name,)).fetchall()
        return [dict(r) for r in rows]


def is_prefilter_rejected(resume_id, job_id):
    with get_conn() as conn:
        row = conn.execute("""
            SELECT id FROM prefilter_rejects WHERE resume_id=? AND job_id=?
        """, (resume_id, job_id)).fetchone()
        return bool(row)


def get_prefilter_reject_resume_ids(job_id):
    """返回 set，便于 pipeline 跳过"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT resume_id FROM prefilter_rejects WHERE job_id=?
        """, (job_id,)).fetchall()
        return {r["resume_id"] for r in rows}


def count_prefilter_rejects(job_id):
    with get_conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS c FROM prefilter_rejects WHERE job_id=?",
            (job_id,)).fetchone()
        return r["c"] if r else 0


def delete_prefilter_rejects_for_job(job_id):
    """删除某岗位的全部预筛拒绝记录。返回删除条数。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM prefilter_rejects WHERE job_id=?", (job_id,))
        return cur.rowcount


def get_prefilter_rejects_by_session(job_id, session_id):
    """返回某批次的预筛拒绝 resume_id 列表。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT resume_id FROM prefilter_rejects WHERE job_id=? AND scrape_session_id=?",
            (job_id, session_id)).fetchall()
        return [r["resume_id"] for r in rows]


def delete_prefilter_rejects_by_session(job_id, session_id):
    """删除某批次的预筛拒绝记录。返回删除条数。"""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM prefilter_rejects WHERE job_id=? AND scrape_session_id=?",
            (job_id, session_id))
        return cur.rowcount


# =============================================================================
# Evaluations
# =============================================================================

def upsert_evaluation(candidate_id, job_id, verdict, pros, cons,
                      has_hard_fail, matches=None, mismatches=None,
                      verdict_reason="", session_id=None):
    """
    pros, cons: list[str]（旧格式，兼容）
    matches: list[{"条件":..., "证据":...}]
    mismatches: list[{"条件":..., "原因":...}]
    session_id: 本次评估批次标识（ISO 时间戳字符串）
    """
    with get_conn() as conn:
        cur = conn.cursor()
        existing = cur.execute("""
            SELECT id FROM evaluations WHERE candidate_id=? AND job_id=?
        """, (candidate_id, job_id)).fetchone()
        params = {
            "candidate_id": candidate_id,
            "job_id": job_id,
            "verdict": verdict,
            "pros_json": json.dumps(pros or [], ensure_ascii=False),
            "cons_json": json.dumps(cons or [], ensure_ascii=False),
            "matches_json": json.dumps(matches or [], ensure_ascii=False) if matches is not None else None,
            "mismatches_json": json.dumps(mismatches or [], ensure_ascii=False) if mismatches is not None else None,
            "verdict_reason": verdict_reason or "",
            "has_hard_fail": 1 if has_hard_fail else 0,
            "scrape_session_id": session_id,
            "now": now_iso(),
        }
        if existing:
            cur.execute("""
                UPDATE evaluations SET verdict=:verdict, pros_json=:pros_json,
                    cons_json=:cons_json, matches_json=:matches_json,
                    mismatches_json=:mismatches_json, verdict_reason=:verdict_reason,
                    has_hard_fail=:has_hard_fail, scrape_session_id=:scrape_session_id,
                    evaluated_at=:now
                WHERE candidate_id=:candidate_id AND job_id=:job_id
            """, params)
            return existing["id"]
        else:
            cur.execute("""
                INSERT INTO evaluations (candidate_id, job_id, verdict, pros_json,
                    cons_json, matches_json, mismatches_json, verdict_reason,
                    has_hard_fail, scrape_session_id, evaluated_at)
                VALUES (:candidate_id, :job_id, :verdict, :pros_json, :cons_json,
                    :matches_json, :mismatches_json, :verdict_reason,
                    :has_hard_fail, :scrape_session_id, :now)
            """, params)
            return cur.lastrowid


def delete_evaluations_for_job(job_id):
    """清空某岗位的所有评估记录（用于重新评估）"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM outcomes WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE job_id=?)
        """, (job_id,))
        cur.execute("""
            DELETE FROM hr_stage_tags WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE job_id=?)
        """, (job_id,))
        cur.execute("""
            DELETE FROM talent_pool WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE job_id=?)
        """, (job_id,))
        cur.execute("DELETE FROM rejection_tags WHERE job_id=?", (job_id,))
        cur.execute("DELETE FROM evaluations WHERE job_id=?", (job_id,))
        cur.execute("DELETE FROM prefilter_rejects WHERE job_id=?", (job_id,))
        return cur.rowcount


def delete_evaluation(evaluation_id):
    """删除单条评估记录（级联清理所有关联表）"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM outcomes WHERE evaluation_id=?", (evaluation_id,))
        cur.execute("DELETE FROM rejection_tags WHERE evaluation_id=?", (evaluation_id,))
        cur.execute("DELETE FROM hr_stage_tags WHERE evaluation_id=?", (evaluation_id,))
        cur.execute("DELETE FROM talent_pool WHERE evaluation_id=?", (evaluation_id,))
        cur.execute("DELETE FROM evaluations WHERE id=?", (evaluation_id,))
        return cur.rowcount


def delete_candidate(candidate_id):
    """删除候选人及其所有评估和操作记录"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM outcomes WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE candidate_id=?)
        """, (candidate_id,))
        cur.execute("""
            DELETE FROM rejection_tags WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE candidate_id=?)
        """, (candidate_id,))
        cur.execute("""
            DELETE FROM hr_stage_tags WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE candidate_id=?)
        """, (candidate_id,))
        cur.execute("""
            DELETE FROM talent_pool WHERE evaluation_id IN
                (SELECT id FROM evaluations WHERE candidate_id=?)
        """, (candidate_id,))
        cur.execute("DELETE FROM evaluations WHERE candidate_id=?", (candidate_id,))
        cur.execute("DELETE FROM candidates WHERE id=?", (candidate_id,))
        return cur.rowcount


def existing_evaluation(candidate_id, job_id):
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM evaluations WHERE candidate_id=? AND job_id=?
        """, (candidate_id, job_id)).fetchone()
        return dict(row) if row else None


def get_evaluation_by_id(evaluation_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM evaluations WHERE id=?",
                           (evaluation_id,)).fetchone()
        return dict(row) if row else None


def update_evaluation_verdict(evaluation_id, verdict, verdict_reason=""):
    """仅更新 verdict 字段，保留 AI 分析结果（matches/mismatches/pros/cons）不变"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE evaluations SET verdict=?, verdict_reason=? WHERE id=?",
            (verdict, verdict_reason, evaluation_id)
        )


def get_latest_outcome_action(evaluation_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT action FROM outcomes WHERE evaluation_id=? ORDER BY action_at DESC LIMIT 1",
            (evaluation_id,)
        ).fetchone()
        return row["action"] if row else None


def get_evaluated_resume_ids(job_id):
    """返回该岗位所有已评估的 resume_id 集合，用于 pipeline 批量跳过"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.resume_id FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            WHERE e.job_id = ?
        """, (job_id,)).fetchall()
        return {r["resume_id"] for r in rows}


def list_evaluations_for_job(job_name, include_hidden=False, exclude_processed=False):
    """
    返回该岗位的所有评估记录，按 verdict 排序（深绿→蓝色→黄→排除）。
    include_hidden=False 时不返回 verdict='排除' 的记录。
    exclude_processed=True 时不返回已有 approved/disapproved/hired/rejected outcome 的记录。
    同时返回 latest_action 与 latest_note。
    """
    verdict_order = {"深绿": 0, "蓝色": 1, "黄色": 2, "排除": 3}
    with get_conn() as conn:
        sql = """
            SELECT e.*, c.resume_id, c.name, c.age, c.first_degree, c.school, c.major,
                   c.english_level, c.total_years, c.raw_html_path, c.duplicate_of_id,
                   (SELECT action FROM outcomes WHERE evaluation_id=e.id
                    ORDER BY action_at DESC LIMIT 1) AS latest_action,
                   (SELECT note FROM outcomes WHERE evaluation_id=e.id
                    ORDER BY action_at DESC LIMIT 1) AS latest_note,
                   j.name AS job_name, j.english_required
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ?
        """
        if not include_hidden:
            sql += " AND e.verdict != '排除'"
        if exclude_processed:
            sql += """
                AND NOT EXISTS (
                    SELECT 1 FROM outcomes
                    WHERE evaluation_id = e.id
                    AND action IN ('approved', 'disapproved', 'hired', 'rejected')
                )"""
        rows = conn.execute(sql, (job_name,)).fetchall()
        results = [dict(r) for r in rows]
    results.sort(key=lambda r: (verdict_order.get(r["verdict"], 99), -r["id"]))
    return results


def get_candidate_cross_job_status(resume_id, exclude_job_name=None):
    """
    返回该候选人在所有岗位上的评估状态，供处理台跨岗位提示使用。
    返回 list[{job_name, verdict, latest_action}]，excluded job 不返回。
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT j.name AS job_name, e.verdict,
                   (SELECT action FROM outcomes WHERE evaluation_id=e.id
                    ORDER BY action_at DESC LIMIT 1) AS latest_action
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE c.resume_id = ?
        """, (resume_id,)).fetchall()
        results = [dict(r) for r in rows]
    if exclude_job_name:
        results = [r for r in results if r["job_name"] != exclude_job_name]
    return results


# =============================================================================
# Outcomes
# =============================================================================

# approved/disapproved 是新增的"通过/不通过"操作，
# 兼容旧的 contacted/interviewed/hired/skipped/rejected
VALID_ACTIONS = {
    "contacted", "interviewed", "hired", "skipped", "rejected",
    "approved", "disapproved",
}

# 视为"正向认可"（用于 Few-Shot 学习）：approved 等同于 hired
POSITIVE_ACTIONS = {"hired", "approved"}
NEGATIVE_ACTIONS = {"rejected", "disapproved"}


def record_outcome(evaluation_id, action, note=None, dwell_seconds=None):
    if action not in VALID_ACTIONS:
        raise ValueError(f"无效操作: {action}，允许 {VALID_ACTIONS}")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO outcomes (evaluation_id, action, action_at, note, dwell_seconds)
            VALUES (?, ?, ?, ?, ?)
        """, (evaluation_id, action, now_iso(), note, dwell_seconds))


def update_latest_note(evaluation_id, note):
    """更新最新一条 outcome 的备注；若没有则插入一条 'note_only' action"""
    with get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute("""
            SELECT id FROM outcomes WHERE evaluation_id=?
            ORDER BY action_at DESC LIMIT 1
        """, (evaluation_id,)).fetchone()
        if row:
            cur.execute("UPDATE outcomes SET note=? WHERE id=?",
                        (note, row["id"]))
        else:
            cur.execute("""
                INSERT INTO outcomes (evaluation_id, action, action_at, note)
                VALUES (?, 'note_only', ?, ?)
            """, (evaluation_id, now_iso(), note))


def list_outcomes(evaluation_id):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM outcomes WHERE evaluation_id=? ORDER BY action_at DESC
        """, (evaluation_id,)).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# Rejection tags（淘汰原因标签库）
# =============================================================================

def add_rejection_tag(evaluation_id, job_id, tag_text):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO rejection_tags (evaluation_id, job_id, tag_text, created_at)
            VALUES (?, ?, ?, ?)
        """, (evaluation_id, job_id, tag_text.strip(), now_iso()))


def list_rejection_tag_stats(job_id=None, threshold=0):
    """统计标签出现频次。返回 list[{tag_text, count, last_at}]，按 count 降序"""
    with get_conn() as conn:
        if job_id:
            rows = conn.execute("""
                SELECT tag_text, COUNT(*) AS cnt, MAX(created_at) AS last_at
                FROM rejection_tags WHERE job_id=?
                GROUP BY tag_text
                HAVING cnt >= ?
                ORDER BY cnt DESC
            """, (job_id, threshold)).fetchall()
        else:
            rows = conn.execute("""
                SELECT tag_text, COUNT(*) AS cnt, MAX(created_at) AS last_at
                FROM rejection_tags
                GROUP BY tag_text
                HAVING cnt >= ?
                ORDER BY cnt DESC
            """, (threshold,)).fetchall()
        return [{"tag_text": r["tag_text"], "count": r["cnt"], "last_at": r["last_at"]}
                for r in rows]


def get_distinct_rejection_tags(limit=50):
    """返回去重后的标签列表，供前端下拉框使用，按使用频次排序"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tag_text, COUNT(*) AS cnt
            FROM rejection_tags
            GROUP BY tag_text
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [r["tag_text"] for r in rows]


# =============================================================================
# Cross-job queries (for natural language query feature)
# =============================================================================

def get_table_schema_summary():
    """返回数据库 schema 的中文描述，供 LLM 转 SQL 使用"""
    return """数据库 schema（SQLite）:

candidates 表（候选人基本信息）:
- id, resume_id, name (姓名), age (年龄), first_degree (第一学历)
- school (学校), major (专业), english_level (英语水平)
- total_years (工作年限), structured_json (完整结构化简历)

jobs 表（岗位）:
- id, name (岗位名称), english_required, min_education, min_years
- industry_required, requires_management

evaluations 表（候选人对岗位的评估）:
- id, candidate_id, job_id, verdict (深绿/蓝色/黄色/排除)
- pros_json, cons_json（旧格式）
- matches_json, mismatches_json, verdict_reason（新格式）
- has_hard_fail (硬性不符合), evaluated_at (评估时间)

outcomes 表（HR 实际操作记录）:
- id, evaluation_id, action (approved/disapproved/contacted/interviewed/hired/skipped/rejected)
- action_at, note
"""


def execute_readonly_sql(sql):
    """执行只读 SQL（自然语言查询使用）"""
    sql_lower = sql.lower().strip()
    forbidden = ["insert ", "update ", "delete ", "drop ", "alter ", "create ", "replace "]
    for kw in forbidden:
        if kw in sql_lower:
            raise ValueError(f"只允许只读查询，禁止 {kw.strip()}")
    with get_conn() as conn:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# 人才池（Talent Pool）
# =============================================================================

POOL_REASON_TAGS = ['在职观望', '薪资差距', '暂无意向', '失联待重试', '地点问题', '其他']


def upsert_talent_pool(evaluation_id, recontact_date, reason_tag='', note=''):
    """放入人才池，或更新已有记录（重置为 active 状态）"""
    with get_conn() as conn:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT id FROM talent_pool WHERE evaluation_id=?",
            (evaluation_id,)).fetchone()
        ts = now_iso()
        if existing:
            cur.execute("""
                UPDATE talent_pool SET recontact_date=?, reason_tag=?, note=?,
                    status='active', updated_at=?
                WHERE evaluation_id=?
            """, (recontact_date, reason_tag, note, ts, evaluation_id))
            return existing["id"]
        else:
            cur.execute("""
                INSERT INTO talent_pool (evaluation_id, recontact_date, reason_tag,
                    note, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)
            """, (evaluation_id, recontact_date, reason_tag, note, ts, ts))
            return cur.lastrowid


def remove_from_talent_pool(evaluation_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM talent_pool WHERE evaluation_id=?", (evaluation_id,))


def get_talent_pool_entry(evaluation_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM talent_pool WHERE evaluation_id=?",
            (evaluation_id,)).fetchone()
        return dict(row) if row else None


def update_talent_pool_status(evaluation_id, status):
    with get_conn() as conn:
        conn.execute("""
            UPDATE talent_pool SET status=?, updated_at=? WHERE evaluation_id=?
        """, (status, now_iso(), evaluation_id))


def list_talent_pool(job_name=None, status='active'):
    """返回人才池列表，含候选人和评估信息，按再联系日期升序"""
    with get_conn() as conn:
        sql = """
            SELECT tp.id AS pool_id, tp.evaluation_id, tp.recontact_date,
                   tp.reason_tag, tp.note, tp.status, tp.created_at,
                   e.verdict, e.verdict_reason,
                   c.name, c.age, c.first_degree, c.school, c.total_years,
                   c.resume_id,
                   j.name AS job_name
            FROM talent_pool tp
            JOIN evaluations e ON tp.evaluation_id = e.id
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE 1=1
        """
        params = []
        if status and status != 'all':
            sql += " AND tp.status = ?"
            params.append(status)
        if job_name:
            sql += " AND j.name = ?"
            params.append(job_name)
        sql += " ORDER BY tp.recontact_date ASC, tp.id ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def list_due_talent_pool(days_ahead=0):
    """返回今天及逾期未联系的记录（status=active 且 recontact_date ≤ 今天+days_ahead）"""
    cutoff = (
        datetime.date.today() + datetime.timedelta(days=days_ahead)
    ).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT tp.id AS pool_id, tp.evaluation_id, tp.recontact_date,
                   tp.reason_tag, tp.note, tp.status,
                   e.verdict,
                   c.name, c.age, c.first_degree, c.school,
                   j.name AS job_name
            FROM talent_pool tp
            JOIN evaluations e ON tp.evaluation_id = e.id
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE tp.status = 'active' AND tp.recontact_date <= ?
            ORDER BY tp.recontact_date ASC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# Scrape session queries
# =============================================================================

def list_sessions_for_job(job_name):
    """返回该岗位所有评估批次，按时间倒序，含每批次 verdict 统计"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.scrape_session_id,
                   MIN(e.evaluated_at) AS session_start,
                   COUNT(*) AS total,
                   SUM(CASE WHEN e.verdict='深绿' THEN 1 ELSE 0 END) AS deep_green,
                   SUM(CASE WHEN e.verdict='蓝色' THEN 1 ELSE 0 END) AS light_green,
                   SUM(CASE WHEN e.verdict='黄色' THEN 1 ELSE 0 END) AS yellow
            FROM evaluations e
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ?
            GROUP BY e.scrape_session_id
            ORDER BY session_start DESC
        """, (job_name,)).fetchall()
        return [dict(r) for r in rows]


def get_resume_ids_for_session(job_name, session_id):
    """返回指定批次的所有 resume_id 集合，用于重启后恢复内存批次"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.resume_id
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ? AND e.scrape_session_id = ?
        """, (job_name, session_id)).fetchall()
        return {r["resume_id"] for r in rows}


# =============================================================================
# Outcome statistics (for learning)
# =============================================================================

def get_outcome_summary(job_name=None):
    """统计各 verdict 下的 outcome 分布，供学习机制使用"""
    with get_conn() as conn:
        if job_name:
            sql = """
                SELECT e.verdict,
                       o.action,
                       COUNT(*) AS cnt
                FROM evaluations e
                JOIN jobs j ON e.job_id = j.id
                LEFT JOIN outcomes o ON o.evaluation_id = e.id
                WHERE j.name = ?
                GROUP BY e.verdict, o.action
            """
            rows = conn.execute(sql, (job_name,)).fetchall()
        else:
            sql = """
                SELECT e.verdict, o.action, COUNT(*) AS cnt
                FROM evaluations e
                LEFT JOIN outcomes o ON o.evaluation_id = e.id
                GROUP BY e.verdict, o.action
            """
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]


def get_hired_candidates(job_name, limit=10):
    """获取某岗位的已录用候选人结构化简历（用于 Few-Shot 学习）
    approved 等同于 hired"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.structured_json, c.name, e.pros_json, e.matches_json
            FROM outcomes o
            JOIN evaluations e ON o.evaluation_id = e.id
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE o.action IN ('hired', 'approved') AND j.name = ?
            ORDER BY o.action_at DESC LIMIT ?
        """, (job_name, limit)).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# Funnel & analytics support
# =============================================================================

def count_evaluations_for_job(job_id, since_iso=None):
    """统计某岗位的评估总数；可选只统计某时间起点之后"""
    with get_conn() as conn:
        if since_iso:
            r = conn.execute("""
                SELECT COUNT(*) AS c FROM evaluations
                WHERE job_id=? AND evaluated_at >= ?
            """, (job_id, since_iso)).fetchone()
        else:
            r = conn.execute("""
                SELECT COUNT(*) AS c FROM evaluations WHERE job_id=?
            """, (job_id,)).fetchone()
        return r["c"] if r else 0


def count_verdicts_for_job(job_id, since_iso=None):
    """返回 {verdict: count} 字典"""
    with get_conn() as conn:
        if since_iso:
            rows = conn.execute("""
                SELECT verdict, COUNT(*) AS c FROM evaluations
                WHERE job_id=? AND evaluated_at >= ?
                GROUP BY verdict
            """, (job_id, since_iso)).fetchall()
        else:
            rows = conn.execute("""
                SELECT verdict, COUNT(*) AS c FROM evaluations
                WHERE job_id=?
                GROUP BY verdict
            """, (job_id,)).fetchall()
        return {r["verdict"]: r["c"] for r in rows}


def get_last_eval_time_for_job(job_id):
    with get_conn() as conn:
        r = conn.execute("""
            SELECT MAX(evaluated_at) AS last_at FROM evaluations WHERE job_id=?
        """, (job_id,)).fetchone()
        return r["last_at"] if r and r["last_at"] else None


def get_action_counts_for_job(job_id, actions=None, since_iso=None):
    """统计某岗位下指定 actions 的数量"""
    with get_conn() as conn:
        sql = """
            SELECT o.action, COUNT(*) AS c FROM outcomes o
            JOIN evaluations e ON o.evaluation_id = e.id
            WHERE e.job_id=?
        """
        params = [job_id]
        if since_iso:
            sql += " AND o.action_at >= ?"
            params.append(since_iso)
        sql += " GROUP BY o.action"
        rows = conn.execute(sql, params).fetchall()
        out = {r["action"]: r["c"] for r in rows}
        if actions:
            return {a: out.get(a, 0) for a in actions}
        return out


# =============================================================================
# 审计查询（供诊断报告使用）
# =============================================================================

def get_calibration_stats(job_id=None):
    """
    AI 校准偏差：统计各 verdict 下 HR 实际操作的分布。
    返回 list[{verdict, action, cnt}]，仅限有明确 approve/reject 操作的记录。
    """
    with get_conn() as conn:
        if job_id:
            rows = conn.execute("""
                SELECT e.verdict, o.action, COUNT(*) AS cnt
                FROM outcomes o
                JOIN evaluations e ON o.evaluation_id = e.id
                WHERE e.job_id = ?
                  AND o.action IN ('approved','hired','disapproved','rejected')
                GROUP BY e.verdict, o.action
            """, (job_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT e.verdict, o.action, COUNT(*) AS cnt
                FROM outcomes o
                JOIN evaluations e ON o.evaluation_id = e.id
                WHERE o.action IN ('approved','hired','disapproved','rejected')
                GROUP BY e.verdict, o.action
            """).fetchall()
        return [dict(r) for r in rows]


def get_yellow_pile_details(job_id):
    """
    黄色堆积风险：返回未处理的黄色候选人列表，含等待天数与在职状态。
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.id AS eval_id, e.evaluated_at, c.name, c.age,
                   c.structured_json
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            WHERE e.job_id = ? AND e.verdict = '黄色'
              AND NOT EXISTS (
                SELECT 1 FROM outcomes WHERE evaluation_id = e.id
                AND action IN ('approved','disapproved','hired','rejected')
              )
            ORDER BY e.evaluated_at ASC
        """, (job_id,)).fetchall()
        return [dict(r) for r in rows]


def get_evidence_quality_stats(job_id):
    """
    证据质量：统计 matches_json 中证据字段是否有实质内容。
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT e.matches_json
            FROM evaluations e
            WHERE e.job_id = ? AND e.matches_json IS NOT NULL
        """, (job_id,)).fetchall()
        return [r["matches_json"] for r in rows]


def get_prefilter_fail_reasons(job_id):
    """返回该岗位所有预筛拒绝原因（原始字符串），供供需矩阵分析。"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT fail_reason FROM prefilter_rejects WHERE job_id = ?
        """, (job_id,)).fetchall()
        return [r["fail_reason"] or "" for r in rows]


def get_mismatch_conditions_for_job(job_id):
    """返回该岗位所有 mismatches_json 中的条件字符串列表，供触发频率分析。"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT mismatches_json FROM evaluations
            WHERE job_id = ? AND mismatches_json IS NOT NULL
        """, (job_id,)).fetchall()
        return [r["mismatches_json"] for r in rows]


def count_total_evaluations_for_job(job_id):
    """含排除在内的全部评估数。"""
    with get_conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE job_id=?",
            (job_id,)).fetchone()
        return r["c"] if r else 0


def count_outcomes_for_job(job_id):
    """统计某岗位已产生的 HR 决策总数（用于偏好学习每20次触发）。"""
    with get_conn() as conn:
        r = conn.execute("""
            SELECT COUNT(*) AS c FROM outcomes o
            JOIN evaluations e ON o.evaluation_id = e.id
            WHERE e.job_id = ?
        """, (job_id,)).fetchone()
        return r["c"] if r else 0


# =============================================================================
# 偏好信号（Preference Learning）
# =============================================================================

def record_preference_signal(evaluation_id, job_id, signal_type, ai_verdict,
                              hr_action, dwell_seconds=None, rejection_tag=None,
                              candidate_snapshot=None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO preference_signals
                (evaluation_id, job_id, signal_type, ai_verdict, hr_action,
                 dwell_seconds, rejection_tag, candidate_snapshot, analyzed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (evaluation_id, job_id, signal_type, ai_verdict, hr_action,
              dwell_seconds, rejection_tag,
              json.dumps(candidate_snapshot, ensure_ascii=False) if candidate_snapshot else None,
              now_iso()))


def count_unanalyzed_signals(job_id):
    with get_conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS c FROM preference_signals WHERE job_id=? AND analyzed=0",
            (job_id,)).fetchone()
        return r["c"] if r else 0


def get_unanalyzed_signals(job_id, limit=20):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM preference_signals
            WHERE job_id=? AND analyzed=0
            ORDER BY created_at ASC LIMIT ?
        """, (job_id, limit)).fetchall()
        return [dict(r) for r in rows]


def mark_signals_analyzed(signal_ids):
    if not signal_ids:
        return
    ph = ",".join("?" * len(signal_ids))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE preference_signals SET analyzed=1 WHERE id IN ({ph})",
            signal_ids)


def list_all_preference_signals_for_job(job_id, limit=50):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ps.*, j.name AS job_name
            FROM preference_signals ps
            JOIN jobs j ON ps.job_id = j.id
            WHERE ps.job_id = ?
            ORDER BY ps.created_at DESC LIMIT ?
        """, (job_id, limit)).fetchall()
        return [dict(r) for r in rows]


def upsert_preference_rule(job_id, rule_text, confidence=0.5, evidence_count=1):
    """插入或更新偏好规则（相同 job_id + rule_text 去重）。"""
    with get_conn() as conn:
        existing = conn.execute("""
            SELECT id, evidence_count FROM preference_rules
            WHERE job_id IS ? AND rule_text=? AND status='pending'
        """, (job_id, rule_text)).fetchone()
        ts = now_iso()
        if existing:
            new_ev = existing["evidence_count"] + evidence_count
            new_conf = min(0.95, confidence + 0.1)
            conn.execute("""
                UPDATE preference_rules SET confidence=?, evidence_count=?, updated_at=?
                WHERE id=?
            """, (new_conf, new_ev, ts, existing["id"]))
            return existing["id"]
        else:
            conn.execute("""
                INSERT INTO preference_rules
                    (job_id, rule_text, confidence, evidence_count, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """, (job_id, rule_text, confidence, evidence_count, ts, ts))


def list_preference_rules(job_id=None, status="pending"):
    with get_conn() as conn:
        if job_id is not None:
            rows = conn.execute("""
                SELECT pr.*, j.name AS job_name
                FROM preference_rules pr
                LEFT JOIN jobs j ON pr.job_id = j.id
                WHERE (pr.job_id = ? OR pr.job_id IS NULL) AND pr.status = ?
                ORDER BY pr.confidence DESC, pr.evidence_count DESC
            """, (job_id, status)).fetchall()
        else:
            rows = conn.execute("""
                SELECT pr.*, j.name AS job_name
                FROM preference_rules pr
                LEFT JOIN jobs j ON pr.job_id = j.id
                WHERE pr.status = ?
                ORDER BY pr.confidence DESC, pr.evidence_count DESC
            """, (status,)).fetchall()
        return [dict(r) for r in rows]


def update_preference_rule_status(rule_id, status):
    with get_conn() as conn:
        conn.execute("""
            UPDATE preference_rules SET status=?, updated_at=? WHERE id=?
        """, (status, now_iso(), rule_id))


def count_preference_signals_total(job_id=None):
    """所有信号数（含已分析）。"""
    with get_conn() as conn:
        if job_id:
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM preference_signals WHERE job_id=?",
                (job_id,)).fetchone()
        else:
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM preference_signals").fetchone()
        return r["c"] if r else 0


def list_preference_signals_recent(job_id=None, limit=30):
    """最近信号，供偏好收件箱展示。"""
    with get_conn() as conn:
        if job_id:
            rows = conn.execute("""
                SELECT ps.*, j.name AS job_name,
                       c.name AS candidate_name
                FROM preference_signals ps
                JOIN jobs j ON ps.job_id = j.id
                JOIN evaluations e ON ps.evaluation_id = e.id
                JOIN candidates c ON e.candidate_id = c.id
                WHERE ps.job_id = ?
                ORDER BY ps.created_at DESC LIMIT ?
            """, (job_id, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT ps.*, j.name AS job_name,
                       c.name AS candidate_name
                FROM preference_signals ps
                JOIN jobs j ON ps.job_id = j.id
                JOIN evaluations e ON ps.evaluation_id = e.id
                JOIN candidates c ON e.candidate_id = c.id
                ORDER BY ps.created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# 能力指纹
# =============================================================================

def update_ability_fingerprint(resume_id: str, fingerprint: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE candidates SET ability_fingerprint=? WHERE resume_id=?",
            (fingerprint, resume_id)
        )


def count_candidates_without_fingerprint() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM candidates "
            "WHERE ability_fingerprint IS NULL OR ability_fingerprint=''"
        ).fetchone()
        return row[0] if row else 0


def get_candidates_without_fingerprint() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, resume_id, name, structured_json FROM candidates "
            "WHERE ability_fingerprint IS NULL OR ability_fingerprint=''"
        ).fetchall()
        return [dict(r) for r in rows]


def search_candidates_by_fingerprint(keywords: list,
                                     source_jobs: list = None,
                                     verdict_filter: list = None,
                                     limit: int = 120) -> list:
    """
    在 ability_fingerprint 中搜索关键词，按命中数量降序返回评估记录列表。
    返回字段与 triage list API 一致，额外含 source_job_name / ability_fingerprint / match_count。
    """
    if not keywords:
        return []

    match_exprs = " + ".join(
        "CASE WHEN c.ability_fingerprint LIKE ? THEN 1 ELSE 0 END"
        for _ in keywords
    )
    match_params = [f"%{kw}%" for kw in keywords]

    or_conditions = " OR ".join(
        "c.ability_fingerprint LIKE ?" for _ in keywords
    )
    where_clauses = [
        "c.ability_fingerprint IS NOT NULL",
        "c.ability_fingerprint != ''",
        f"({or_conditions})",
    ]
    where_params = [f"%{kw}%" for kw in keywords]

    if source_jobs:
        ph = ",".join("?" * len(source_jobs))
        where_clauses.append(f"j.name IN ({ph})")
        where_params.extend(source_jobs)

    if verdict_filter:
        ph = ",".join("?" * len(verdict_filter))
        where_clauses.append(f"e.verdict IN ({ph})")
        where_params.extend(verdict_filter)

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT
            e.id            AS evaluation_id,
            c.id            AS candidate_id,
            c.resume_id,
            c.name,
            c.age,
            c.first_degree,
            c.school,
            c.total_years,
            c.duplicate_of_id,
            c.ability_fingerprint,
            e.verdict,
            e.verdict_reason,
            e.matches_json,
            e.mismatches_json,
            e.scrape_session_id,
            j.name          AS source_job_name,
            ({match_exprs}) AS match_count,
            (
                SELECT o.action FROM outcomes o
                WHERE o.evaluation_id = e.id
                ORDER BY o.action_at DESC LIMIT 1
            ) AS latest_action
        FROM candidates c
        JOIN evaluations e ON e.candidate_id = c.id
        JOIN jobs j        ON e.job_id = j.id
        WHERE {where_sql}
        ORDER BY match_count DESC, e.evaluated_at DESC
        LIMIT ?
    """

    all_params = match_params + where_params + [limit]

    with get_conn() as conn:
        rows = conn.execute(sql, all_params).fetchall()

    results = []
    for row in rows:
        r = dict(row)
        try:
            r["matches"] = json.loads(r.pop("matches_json") or "[]")
        except Exception:
            r["matches"] = []
        try:
            r["mismatches"] = json.loads(r.pop("mismatches_json") or "[]")
        except Exception:
            r["mismatches"] = []
        results.append(r)
    return results


# =============================================================================
# 通过/不通过简历查询（需求7）
# =============================================================================

HR_STAGE_ORDER = ['已联系', '拒绝', '已约面', '已一面', '已二面', '谈薪中', '签批中', '已入职']
REJECT_REASONS = ['薪酬', '加班', '距离', '福利', '家庭', '其他']


def _eval_base_sql():
    return """
        SELECT e.id AS evaluation_id, e.candidate_id, e.verdict, e.verdict_reason,
               e.pros_json, e.cons_json, e.matches_json, e.mismatches_json,
               c.resume_id, c.name, c.age, c.first_degree, c.school, c.major,
               c.english_level, c.total_years, c.raw_html_path, c.duplicate_of_id,
               o.action_at AS decision_at,
               o.note AS outcome_note
        FROM outcomes o
        JOIN evaluations e ON o.evaluation_id = e.id
        JOIN candidates c ON e.candidate_id = c.id
        JOIN jobs j ON e.job_id = j.id
    """


def list_approved_for_job(job_name, date_from=None, date_to=None):
    """返回该岗位最终状态为通过（approved/hired）的候选人，按决定时间倒序。
    每个 evaluation 只取一行：最近的 approved/hired outcome，且其后无更晚的决定性操作。
    """
    with get_conn() as conn:
        sql = _eval_base_sql() + """
            WHERE j.name = ?
              AND o.action IN ('approved', 'hired')
              AND NOT EXISTS (
                SELECT 1 FROM outcomes o2
                WHERE o2.evaluation_id = o.evaluation_id
                  AND o2.id > o.id
                  AND o2.action IN ('approved', 'hired', 'disapproved', 'rejected')
              )
        """
        params = [job_name]
        if date_from:
            sql += " AND o.action_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND o.action_at <= ?"
            params.append(date_to + "T23:59:59")
        sql += " ORDER BY o.action_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def list_disapproved_for_job(job_name):
    """返回该岗位最终状态为不通过（disapproved/rejected）的候选人，按决定时间倒序。
    每个 evaluation 只取一行：最近的 disapproved/rejected outcome，且其后无更晚的决定性操作。
    """
    with get_conn() as conn:
        sql = _eval_base_sql() + """
            WHERE j.name = ?
              AND o.action IN ('disapproved', 'rejected')
              AND NOT EXISTS (
                SELECT 1 FROM outcomes o2
                WHERE o2.evaluation_id = o.evaluation_id
                  AND o2.id > o.id
                  AND o2.action IN ('approved', 'hired', 'disapproved', 'rejected')
              )
            ORDER BY o.action_at DESC
        """
        rows = conn.execute(sql, [job_name]).fetchall()
        return [dict(r) for r in rows]


# =============================================================================
# HR 阶段标签（需求8/9）
# =============================================================================

def get_hr_stage(evaluation_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM hr_stage_tags WHERE evaluation_id=?",
            (evaluation_id,)).fetchone()
        return dict(row) if row else None


def upsert_hr_stage(evaluation_id, job_id, stages=None, reject_reason='', note=''):
    """保存或更新候选人的 HR 阶段标签。"""
    stages_json = json.dumps(stages or [], ensure_ascii=False)
    ts = now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM hr_stage_tags WHERE evaluation_id=?",
            (evaluation_id,)).fetchone()
        if existing:
            conn.execute("""
                UPDATE hr_stage_tags
                SET stages_json=?, reject_reason=?, note=?, updated_at=?
                WHERE evaluation_id=?
            """, (stages_json, reject_reason, note, ts, evaluation_id))
        else:
            conn.execute("""
                INSERT INTO hr_stage_tags
                    (evaluation_id, job_id, stages_json, reject_reason, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (evaluation_id, job_id, stages_json, reject_reason, note, ts))


def update_hr_note(evaluation_id, note):
    """仅更新不通过页面备注，不改变阶段标签。"""
    ts = now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM hr_stage_tags WHERE evaluation_id=?",
            (evaluation_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE hr_stage_tags SET note=?, updated_at=? WHERE evaluation_id=?",
                (note, ts, evaluation_id))
        else:
            conn.execute("""
                INSERT INTO hr_stage_tags
                    (evaluation_id, job_id, stages_json, reject_reason, note, updated_at)
                VALUES (?, (SELECT job_id FROM evaluations WHERE id=?), '[]', '', ?, ?)
            """, (evaluation_id, evaluation_id, note, ts))


def get_hr_stages_for_job(job_name):
    """批量获取某岗位所有候选人的 HR 阶段标签，返回 {evaluation_id: hr_stage_row}。"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ht.*
            FROM hr_stage_tags ht
            JOIN evaluations e ON ht.evaluation_id = e.id
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ?
        """, (job_name,)).fetchall()
        return {r["evaluation_id"]: dict(r) for r in rows}


def count_hr_stages_for_job(job_name):
    """统计各阶段人数，返回 {stage: count}。"""
    stage_map = get_hr_stages_for_job(job_name)
    counts = {s: 0 for s in HR_STAGE_ORDER}
    counts['无标签'] = 0
    for row in stage_map.values():
        try:
            stages = json.loads(row.get('stages_json') or '[]')
        except Exception:
            stages = []
        if not stages:
            counts['无标签'] += 1
        else:
            for s in stages:
                if s in counts:
                    counts[s] += 1
    return counts


# =============================================================================
# 抓取批次（scrape_sessions）
# =============================================================================

def upsert_scrape_session(session_id, job_name, device_name=""):
    """创建或更新抓取批次记录。"""
    job = get_job(job_name)
    if not job:
        return
    ts = now_iso()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM scrape_sessions WHERE id=?", (session_id,)).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO scrape_sessions (id, job_id, job_name, device_name, started_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, job["id"], job_name, device_name, ts))


def finish_scrape_session(session_id):
    """标记批次结束，并统计该批次的各项计数。"""
    ts = now_iso()
    with get_conn() as conn:
        # 本批次抓取总数 = evaluations + prefilter_rejects
        ev_count = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE scrape_session_id=?",
            (session_id,)).fetchone()["c"]
        pf_count = conn.execute(
            "SELECT COUNT(*) AS c FROM prefilter_rejects WHERE scrape_session_id=?",
            (session_id,)).fetchone()["c"]
        excl_count = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE scrape_session_id=? AND verdict='排除'",
            (session_id,)).fetchone()["c"]
        passed_count = ev_count - excl_count

        conn.execute("""
            UPDATE scrape_sessions
            SET finished_at=?, total_scraped=?, passed_prefilter=?,
                ai_excluded=?, ai_passed=?
            WHERE id=?
        """, (ts, ev_count + pf_count, ev_count, excl_count, passed_count, session_id))


def list_scrape_sessions(job_name, limit=10):
    """返回某岗位最近 N 次抓取批次，按时间倒序。"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM scrape_sessions
            WHERE job_name=?
            ORDER BY started_at DESC LIMIT ?
        """, (job_name, limit)).fetchall()
        return [dict(r) for r in rows]


def get_batch_stats(job_name, session_id):
    """返回单个批次的简历处理进度。"""
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE scrape_session_id=?",
            (session_id,)).fetchone()["c"]
        pf = conn.execute(
            "SELECT COUNT(*) AS c FROM prefilter_rejects WHERE scrape_session_id=?",
            (session_id,)).fetchone()["c"]
        excl = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluations WHERE scrape_session_id=? AND verdict='排除'",
            (session_id,)).fetchone()["c"]
        approved = conn.execute("""
            SELECT COUNT(*) AS c FROM outcomes o
            JOIN evaluations e ON o.evaluation_id = e.id
            WHERE e.scrape_session_id=? AND o.action IN ('approved','hired')
        """, (session_id,)).fetchone()["c"]
        disapproved = conn.execute("""
            SELECT COUNT(*) AS c FROM outcomes o
            JOIN evaluations e ON o.evaluation_id = e.id
            WHERE e.scrape_session_id=? AND o.action IN ('disapproved','rejected')
        """, (session_id,)).fetchone()["c"]
    return {
        "session_id": session_id,
        "job_name": job_name,
        "total_scraped": total + pf,
        "prefilter_rejected": pf,
        "ai_processed": total,
        "ai_excluded": excl,
        "ai_passed": total - excl,
        "hr_approved": approved,
        "hr_disapproved": disapproved,
        "hr_pending": (total - excl) - approved - disapproved,
    }


# =============================================================================
# Correction signals & criteria notes
# =============================================================================

def record_correction_signal(job_id, eval_id, direction, condition_name,
                              error_type, evidence_text, hr_note):
    """记录一条纠错信号。direction: 'too_loose'|'too_strict'"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO correction_signals
              (job_id, eval_id, direction, condition_name, error_type,
               evidence_text, hr_note, analyzed)
            VALUES (?,?,?,?,?,?,?,0)
        """, (job_id, eval_id, direction, condition_name, error_type,
              evidence_text, hr_note))


def get_unanalyzed_correction_signals(job_id, limit=50):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM correction_signals
            WHERE job_id=? AND analyzed=0
            ORDER BY created_at
            LIMIT ?
        """, (job_id, limit)).fetchall()
        return [dict(r) for r in rows]


def mark_correction_signals_analyzed(signal_ids):
    if not signal_ids:
        return
    placeholders = ",".join("?" * len(signal_ids))
    with get_conn() as conn:
        conn.execute(
            f"UPDATE correction_signals SET analyzed=1 WHERE id IN ({placeholders})",
            signal_ids
        )


def get_condition_correction_counts(job_id):
    """返回各条件的纠错次数，used to decide when to generate a note."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT condition_name, direction, COUNT(*) AS cnt
            FROM correction_signals
            WHERE job_id=? AND analyzed=0
            GROUP BY condition_name, direction
            HAVING cnt >= 3
        """, (job_id,)).fetchall()
        return [dict(r) for r in rows]


def get_correction_signals_for_condition(job_id, condition_name, direction, limit=10):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM correction_signals
            WHERE job_id=? AND condition_name=? AND direction=?
            ORDER BY created_at DESC LIMIT ?
        """, (job_id, condition_name, direction, limit)).fetchall()
        return [dict(r) for r in rows]


def upsert_criteria_note(job_id, condition_name, note_text, is_hard,
                         source_signal_count):
    """插入或更新为 pending 状态的草稿细则（已 confirmed 的不覆盖）。"""
    with get_conn() as conn:
        existing = conn.execute("""
            SELECT id, status FROM job_criteria_notes
            WHERE job_id=? AND condition_name=?
            ORDER BY created_at DESC LIMIT 1
        """, (job_id, condition_name)).fetchone()
        if existing and existing["status"] == "confirmed":
            return existing["id"]
        if existing and existing["status"] == "pending":
            conn.execute("""
                UPDATE job_criteria_notes
                SET note_text=?, is_hard=?, source_signal_count=?
                WHERE id=?
            """, (note_text, int(is_hard), source_signal_count, existing["id"]))
            return existing["id"]
        conn.execute("""
            INSERT INTO job_criteria_notes
              (job_id, condition_name, note_text, is_hard, status, source_signal_count)
            VALUES (?,?,?,?,?,?)
        """, (job_id, condition_name, note_text, int(is_hard),
              "pending", source_signal_count))


def list_criteria_notes(job_id, status=None):
    with get_conn() as conn:
        if status:
            rows = conn.execute("""
                SELECT * FROM job_criteria_notes
                WHERE job_id=? AND status=?
                ORDER BY created_at DESC
            """, (job_id, status)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM job_criteria_notes
                WHERE job_id=?
                ORDER BY status DESC, created_at DESC
            """, (job_id,)).fetchall()
        return [dict(r) for r in rows]


def get_confirmed_criteria_notes(job_id):
    return list_criteria_notes(job_id, status="confirmed")


def confirm_criteria_note(note_id, note_text, is_hard):
    """HR确认一条细则，可同时修改文字和硬软性。"""
    with get_conn() as conn:
        conn.execute("""
            UPDATE job_criteria_notes
            SET status='confirmed', note_text=?, is_hard=?,
                confirmed_at=datetime('now')
            WHERE id=?
        """, (note_text, int(is_hard), note_id))


def dismiss_criteria_note(note_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_criteria_notes SET status='dismissed' WHERE id=?",
            (note_id,)
        )


def get_criteria_override_stats(job_id):
    """统计深绿被否率和黄色被通过率（全时段，用于 criteria.html 快速概览）。"""
    with get_conn() as conn:
        dg = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN o.action IN ('disapproved','rejected') THEN 1 ELSE 0 END) AS rejected
            FROM evaluations e
            JOIN outcomes o ON o.evaluation_id = e.id
            WHERE e.job_id=? AND e.verdict='深绿'
        """, (job_id,)).fetchone()
        yw = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN o.action IN ('approved','hired') THEN 1 ELSE 0 END) AS approved
            FROM evaluations e
            JOIN outcomes o ON o.evaluation_id = e.id
            WHERE e.job_id=? AND e.verdict='黄色'
        """, (job_id,)).fetchone()
    dg_total = dg["total"] or 0
    yw_total = yw["total"] or 0
    return {
        "dg_total": dg_total,
        "dg_reject_rate": round(dg["rejected"] / dg_total * 100, 1) if dg_total else 0,
        "yw_total": yw_total,
        "yw_approve_rate": round(yw["approved"] / yw_total * 100, 1) if yw_total else 0,
    }


def get_criteria_effect_stats(job_id):
    """
    模块5 效果追踪：以第一条细则被确认的时间为分界点，
    对比细则加入前后的深绿被否率和黄色被通过率。
    返回 {split_date, before: {...}, after: {...} | None}
    """
    with get_conn() as conn:
        split_row = conn.execute("""
            SELECT MIN(confirmed_at) AS first_confirmed
            FROM job_criteria_notes
            WHERE job_id=? AND status='confirmed' AND confirmed_at IS NOT NULL
        """, (job_id,)).fetchone()
        split_date = split_row["first_confirmed"] if split_row else None

        def _rates(extra_sql, extra_params):
            dg = conn.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN o.action IN ('disapproved','rejected') THEN 1 ELSE 0 END) AS rejected
                FROM evaluations e
                JOIN outcomes o ON o.evaluation_id = e.id
                WHERE e.job_id=? AND e.verdict='深绿' {extra_sql}
            """, [job_id] + extra_params).fetchone()
            yw = conn.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN o.action IN ('approved','hired') THEN 1 ELSE 0 END) AS approved
                FROM evaluations e
                JOIN outcomes o ON o.evaluation_id = e.id
                WHERE e.job_id=? AND e.verdict='黄色' {extra_sql}
            """, [job_id] + extra_params).fetchone()
            dg_t = dg["total"] or 0
            yw_t = yw["total"] or 0
            return {
                "dg_total": dg_t,
                "dg_reject_rate": round(dg["rejected"] / dg_t * 100, 1) if dg_t else None,
                "yw_total": yw_t,
                "yw_approve_rate": round(yw["approved"] / yw_t * 100, 1) if yw_t else None,
            }

        if split_date:
            before = _rates("AND o.action_at < ?", [split_date])
            after  = _rates("AND o.action_at >= ?", [split_date])
        else:
            before = _rates("", [])
            after  = None

    return {"split_date": split_date, "before": before, "after": after}


def get_all_condition_signal_counts(job_id):
    """
    模块2 错误模式库：返回该岗位所有纠错条件的累计信号数，
    含已分析和未分析（供 UI 展示积累进度，不受 analyzed 标志影响）。
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT condition_name, direction,
                   COUNT(*) AS total_count,
                   SUM(CASE WHEN analyzed=0 THEN 1 ELSE 0 END) AS pending_count,
                   MAX(created_at) AS latest_at
            FROM correction_signals
            WHERE job_id=?
            GROUP BY condition_name, direction
            ORDER BY total_count DESC
        """, (job_id,)).fetchall()
        return [dict(r) for r in rows]


def get_stale_criteria_notes(job_id, days=30):
    """
    模块6 定期细则整理：返回已确认但超过 days 天未复查的细则。
    默认 30 天（每月整理周期）。
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT *,
                   CAST(julianday('now') - julianday(confirmed_at) AS INTEGER) AS days_since_review
            FROM job_criteria_notes
            WHERE job_id=? AND status='confirmed'
              AND confirmed_at IS NOT NULL
              AND julianday('now') - julianday(confirmed_at) > ?
            ORDER BY confirmed_at ASC
        """, (job_id, days)).fetchall()
        return [dict(r) for r in rows]


def refresh_criteria_note_reviewed(note_id):
    """模块6：HR 复查后刷新细则的 confirmed_at，重置 30 天计时。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_criteria_notes SET confirmed_at=datetime('now') WHERE id=?",
            (note_id,)
        )


if __name__ == "__main__":
    init_db()
    print(f"DB: {DB_PATH}")
    print(get_table_schema_summary())
