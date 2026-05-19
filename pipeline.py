"""
评估管线（v2 版）
================
HTML文件 → 结构化提取 → 入库 candidates
        → 规则预过滤 → 若失败：写入 prefilter_rejects（轻量记录，不调 LLM）
        → LLM 判断 → 写入 evaluations（深绿/蓝色/黄色）

        → 处理完后压缩 HTML 节省磁盘

特性：
- 跨岗位去重（同一 resume_id 只在 candidates 表存一份）
- 同岗位幂等（已评估的简历直接跳过；可通过 reset_evaluations 清空后重跑）
- prefilter 失败不进入 LLM 阶段，下次扫描直接跳过该 resume_id
- 内存检查点：中断恢复时跳过"处理中"条目
- HTML 文件 gzip 压缩（节省磁盘 ~80%）
- Ghost 候选人检测：(姓名+学校+学历) 高度相似时标记 duplicate_of_id
- 进度日志（写到 logs/<job_name>.log，由 Web 轮询读取）
- 支持中途停止（stop_flag）
"""

import os
import sys
import gzip
import json
import shutil
import logging
import threading
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import database
import extractor
import judger
import prefilter
import llm_utils

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)

MAX_EVAL_WORKERS = 5   # 并发评估线程数（对应同时发起的 LLM 请求数）

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)


# =============================================================================
# 状态管理（供 Web 轮询）
# =============================================================================

_state = {}
_state_lock = threading.Lock()

# 内存检查点：用于断点续传，记录(job_name, resume_id) → "processing"
_checkpoint = {}
_checkpoint_lock = threading.Lock()

# 错误事件队列（前端轮询 /api/job/errors 获取，用于实时错误通知）
_errors: dict = {}
_error_lock = threading.Lock()


def _push_error(job_name, msg, phase="", resume_id=""):
    event = {
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
        "msg": msg,
        "phase": phase,
        "resume_id": resume_id,
    }
    with _error_lock:
        lst = _errors.setdefault(job_name, [])
        lst.append(event)
        if len(lst) > 100:
            lst[:] = lst[-100:]


def get_errors(job_name, since=0):
    with _error_lock:
        lst = _errors.get(job_name, [])
        return list(lst[since:]), len(lst)


def clear_errors(job_name):
    with _error_lock:
        _errors.pop(job_name, None)


def safe_filename(s):
    """统一处理岗位名中的特殊字符（与 init_jobs.safe_dirname 一致）"""
    return (s.replace("/", "-")
             .replace("\\", "-")
             .replace(":", "-"))


def _default_state():
    return {
        "running": False,
        "phase": "idle",       # idle / scraping / evaluating / done
        "current": 0,
        "total": 0,
        "last_msg": "尚未开始",
        "done": False,
        "error": None,
        "stop_requested": False,
        "skip_count": 0,
    }


def _set_state(job_name, **kwargs):
    with _state_lock:
        st = _state.setdefault(job_name, _default_state())
        st.update(kwargs)


def get_state(job_name):
    with _state_lock:
        return dict(_state.get(job_name, _default_state()))


def request_stop(job_name):
    """请求停止当前任务（抓取或评估）"""
    with _state_lock:
        if job_name in _state and _state[job_name].get("running"):
            _state[job_name]["stop_requested"] = True
            return True
    return False


def is_stop_requested(job_name):
    with _state_lock:
        st = _state.get(job_name)
        return bool(st and st.get("stop_requested"))


def reset_state(job_name):
    with _state_lock:
        _state[job_name] = _default_state()
    with _checkpoint_lock:
        _checkpoint.pop(job_name, None)


# =============================================================================
# 检查点
# =============================================================================

def _checkpoint_mark(job_name, resume_id):
    with _checkpoint_lock:
        _checkpoint.setdefault(job_name, set()).add(resume_id)


def _checkpoint_clear(job_name, resume_id):
    with _checkpoint_lock:
        s = _checkpoint.get(job_name)
        if s and resume_id in s:
            s.discard(resume_id)


def _checkpoint_in_progress(job_name, resume_id):
    with _checkpoint_lock:
        return resume_id in _checkpoint.get(job_name, set())


# =============================================================================
# 日志
# =============================================================================

def _log(job_name, msg):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    log_path = os.path.join(LOG_DIR, f"{safe_filename(job_name)}.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    _set_state(job_name, last_msg=line)
    logger.info(line)


def reset_log(job_name):
    log_path = os.path.join(LOG_DIR, f"{safe_filename(job_name)}.log")
    try:
        open(log_path, "w").close()
    except Exception:
        pass


def read_log_tail(job_name, max_lines=200):
    log_path = os.path.join(LOG_DIR, f"{safe_filename(job_name)}.log")
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception:
        return ""


# =============================================================================
# HTML 压缩 / 读取
# =============================================================================

def _compress_html(html_path):
    """LLM 评估完成后压缩 HTML，节省磁盘空间。失败不影响主流程"""
    try:
        if not html_path or not os.path.exists(html_path):
            return None
        if html_path.endswith(".gz"):
            return html_path
        gz_path = html_path + ".gz"
        with open(html_path, "rb") as fin, gzip.open(gz_path, "wb",
                                                    compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        os.remove(html_path)
        return gz_path
    except Exception as e:
        logger.warning(f"压缩 HTML 失败 {html_path}: {e}")
        return html_path


def _delete_html(html_path):
    """硬性条件不符合时直接删除 HTML 文件，释放磁盘并清洁工作区"""
    try:
        if not html_path:
            return
        for p in [html_path, html_path + ".gz"]:
            if os.path.exists(p):
                os.remove(p)
    except Exception as e:
        logger.warning(f"删除 HTML 失败 {html_path}: {e}")


def read_html_any(path):
    """统一读取 HTML，支持原始与 .gz"""
    if not path:
        return None
    # 优先精确匹配
    if os.path.exists(path):
        if path.endswith(".gz"):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                with gzip.open(path, "rb") as f:
                    return f.read().decode("utf-8", errors="replace")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    # 尝试 .gz
    if not path.endswith(".gz") and os.path.exists(path + ".gz"):
        with gzip.open(path + ".gz", "rt", encoding="utf-8") as f:
            return f.read()
    # 尝试去 .gz
    if path.endswith(".gz"):
        plain = path[:-3]
        if os.path.exists(plain):
            with open(plain, "r", encoding="utf-8") as f:
                return f.read()
    return None


# =============================================================================
# Ghost 候选人检测
# =============================================================================

def _normalize(s):
    return (s or "").strip().lower().replace(" ", "")


def _detect_duplicate(structured, current_resume_id):
    """
    用 (姓名+学校+第一学历) 模糊匹配数据库中已有候选人。
    返回 candidate.id（疑似重复的原始候选人）或 None。
    """
    if not isinstance(structured, dict):
        return None
    name = _normalize(structured.get("name"))
    school = _normalize(structured.get("school"))
    first_degree = _normalize(structured.get("first_degree"))
    if not name or not school:
        return None

    try:
        all_cands = database.list_all_candidates_min()
    except Exception:
        return None
    for c in all_cands:
        if c.get("resume_id") == current_resume_id:
            continue
        cn = _normalize(c.get("name"))
        cs = _normalize(c.get("school"))
        cd = _normalize(c.get("first_degree"))
        if cn == name and cs == school:
            # 学校 + 姓名相同就够强
            if not first_degree or not cd or first_degree == cd:
                return c["id"]
    return None


# =============================================================================
# 单份简历处理
# =============================================================================

def process_resume(client, html_path, name_hint, resume_id, job_record,
                   session_id=None):
    """
    处理单份简历，写入数据库。
    返回 (evaluation_id, verdict) 或 (None, None) 表示失败。
    """
    job_name = job_record["name"]
    job_config = json.loads(job_record["config_json"])
    job_prompt = job_record["prompt_text"]
    job_id = job_record["id"]

    # 1. 检查候选人是否已评估
    existing_candidate = database.get_candidate_by_resume_id(resume_id)
    if existing_candidate:
        existing_eval = database.existing_evaluation(existing_candidate["id"], job_id)
        if existing_eval:
            _log(job_name, f"  ↩ 简历 {resume_id} 在本岗位已有评估记录（verdict={existing_eval['verdict']}），跳过分析")
            return existing_eval["id"], existing_eval["verdict"]

    # 1.5 检查是否已 prefilter 拒绝
    if database.is_prefilter_rejected(resume_id, job_id):
        return None, "排除"

    # 内存检查点
    if _checkpoint_in_progress(job_name, resume_id):
        return None, None
    _checkpoint_mark(job_name, resume_id)

    try:
        # 2. 读 HTML（兼容压缩文件）
        html_raw = read_html_any(html_path)
        if not html_raw:
            _log(job_name, f"  ✗ HTML 文件读取失败: {os.path.basename(str(html_path))}")
            _push_error(job_name, f"HTML 文件读取失败: {os.path.basename(str(html_path))}", phase="HTML读取", resume_id=resume_id)
            return None, None

        # 3. 结构化提取（如果之前提取过则复用）
        if existing_candidate and existing_candidate.get("structured_json"):
            try:
                structured = json.loads(existing_candidate["structured_json"])
            except Exception:
                structured = extractor.extract_with_retry(client, html_raw)
        else:
            structured = extractor.extract_with_retry(client, html_raw)

        if not structured:
            _log(job_name, f"  ✗ 简历结构化提取失败（LLM 解析出错），跳过 {name_hint}")
            _push_error(job_name, f"简历结构化提取失败（LLM 解析出错）: {name_hint}", phase="结构化提取", resume_id=resume_id)
            return None, None

        # 兜底：填入文件名中的姓名
        if not structured.get("name") and name_hint:
            structured["name"] = name_hint

        # 4. 入库 / 更新 candidate
        cand_info = {
            "name": structured.get("name") or name_hint,
            "age": structured.get("age"),
            "first_degree": structured.get("first_degree"),
            "school": structured.get("school"),
            "major": structured.get("major"),
            "english_level": structured.get("english_level"),
            "total_years": structured.get("total_work_years"),
            "raw_html_path": html_path,
            "structured_json": structured,
            "duplicate_of_id": None,
        }
        candidate_id = database.upsert_candidate(resume_id, cand_info)

        # 6. 预过滤（失败时仅写入轻量 prefilter_rejects 表，**不调 LLM**）
        pf = prefilter.prefilter(structured, job_config, job_name=job_name)
        if not pf["passed"]:
            reason = "; ".join(pf["hard_fail_reasons"])
            _log(job_name, f"  预过滤排除：{reason}")
            database.upsert_prefilter_reject(resume_id, job_id, reason,
                                             name_hint=structured.get("name") or name_hint,
                                             session_id=session_id)
            _delete_html(html_path)   # 硬性条件不符合，直接删除文件
            return None, "排除"

        # 6.5 能力指纹生成（预筛通过后，LLM判断前；已有指纹则复用）
        existing_fp = (existing_candidate or {}).get("ability_fingerprint")
        if not existing_fp:
            try:
                fp = extractor.generate_fingerprint(structured, client)
                if fp:
                    database.update_ability_fingerprint(resume_id, fp)
                    _log(job_name, f"  ✓ 能力指纹已生成（{len(fp)}字）")
            except Exception as _fp_err:
                logger.warning(f"指纹生成失败 ({resume_id}): {_fp_err}")

        # 7. LLM 判断
        judgment = judger.judge_with_retry(client, structured, job_prompt, job_name)
        if not judgment:
            _push_error(job_name, f"LLM 判断失败（无返回结果）: {name_hint}", phase="LLM判断", resume_id=resume_id)
            return None, None

        eval_id = database.upsert_evaluation(
            candidate_id, job_id,
            verdict=judgment["verdict"],
            pros=judgment.get("pros", []),
            cons=judgment.get("cons", []),
            has_hard_fail=judgment.get("has_hard_fail", False),
            matches=judgment.get("符合"),
            mismatches=judgment.get("不符合"),
            verdict_reason=judgment.get("verdict_reason", ""),
            session_id=session_id,
        )

        # 9. 压缩 HTML
        _compress_html(html_path)

        return eval_id, judgment["verdict"]
    finally:
        _checkpoint_clear(job_name, resume_id)


# =============================================================================
# 批量评估
# =============================================================================

def run_evaluation(job_name, force_reevaluate=False):
    """
    评估指定岗位的所有简历（位于 工作区_<job_name>/简历原始文件/）。
    可在线程中调用。
    force_reevaluate=True 时先清空该岗位旧评估再重跑。
    """
    session_id = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    _set_state(job_name, running=True, phase="evaluating",
               current=0, total=0, last_msg="开始评估",
               done=False, error=None, stop_requested=False)

    try:
        client = llm_utils.initialize_client()
        if not client:
            _set_state(job_name, running=False, done=True,
                       error="LLM 客户端不可用：请确认 .env 文件包含 DEEPSEEK_API_KEY")
            return

        job_record = database.get_job(job_name)
        if not job_record:
            _set_state(job_name, running=False, done=True,
                       error=f"找不到岗位 {job_name}")
            return

        job_id = job_record["id"]

        # 重新评估：清空旧记录
        if force_reevaluate:
            n = database.delete_evaluations_for_job(job_id)
            _log(job_name, f"重新评估：已清空 {n} 条旧记录")

        html_dir = _resolve_workspace_dir(job_name)
        if not html_dir or not os.path.exists(html_dir):
            _set_state(job_name, running=False, done=True,
                       error=f"找不到简历目录，请先抓取简历")
            return

        # 收集所有 HTML 文件（含 .gz）
        all_files = sorted([
            f for f in os.listdir(html_dir)
            if f.endswith(".html") or f.endswith(".html.gz")
        ])
        if not all_files:
            _log(job_name, "目录中没有 HTML 文件")
            _set_state(job_name, running=False, done=True, phase="done")
            return

        # 批量预查询：已评估 / 已 prefilter 拒绝
        already_evaluated = database.get_evaluated_resume_ids(job_id)
        already_prefilter_rejected = database.get_prefilter_reject_resume_ids(job_id)
        already_done = already_evaluated | already_prefilter_rejected

        # 解析每个文件的 resume_id 并分类
        new_files = []
        skipped_count = 0
        for fname in all_files:
            rid, _ = _parse_html_filename(fname)
            if rid in already_done:
                skipped_count += 1
            else:
                new_files.append((fname, rid))

        total_files = len(all_files)
        new_count = len(new_files)

        _set_state(job_name, skip_count=skipped_count)
        _log(job_name, f"共发现 {total_files} 份简历："
                       f"待新评估 {new_count} 份，已处理跳过 {skipped_count} 份")

        if new_count == 0:
            _log(job_name, "✓ 没有新简历需要评估")
            _set_state(job_name, running=False, done=True,
                       phase="done", total=0)
            return

        _set_state(job_name, total=new_count, current=0)
        _log(job_name, f"并发评估（{MAX_EVAL_WORKERS} 线程），共 {new_count} 份")

        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS,
                                thread_name_prefix=f"eval-{job_name[:8]}") as ex:
            futs = {}
            for fname, rid in new_files:
                _, name_hint = _parse_html_filename(fname)
                fut = ex.submit(process_resume, client,
                                os.path.join(html_dir, fname),
                                name_hint, rid, job_record,
                                session_id=session_id)
                futs[fut] = name_hint
            for fut in as_completed(futs):
                if is_stop_requested(job_name):
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                name_hint = futs[fut]
                try:
                    _, verdict = fut.result()
                except Exception as e:
                    verdict = None
                    _log(job_name, f"  ⚠ 处理异常 {name_hint}: {e}")
                    _push_error(job_name, f"处理异常 {name_hint}: {e}", phase="评估异常")
                    logger.exception(e)
                completed += 1
                _set_state(job_name, current=completed)
                if verdict == "排除":
                    _log(job_name, f"  ({completed}/{new_count}) {name_hint} → 排除")
                elif verdict:
                    _log(job_name, f"  ({completed}/{new_count}) {name_hint} → {verdict}")
                else:
                    _log(job_name, f"  ({completed}/{new_count}) {name_hint} ✗ 处理失败")

        if is_stop_requested(job_name):
            _set_state(job_name, running=False, done=True,
                       phase="done", last_msg="已停止")
        else:
            _log(job_name, f"✓ 全部完成（新评估 {completed} 份）")
            _set_state(job_name, running=False, done=True, phase="done")
    except Exception as e:
        logger.exception(e)
        _set_state(job_name, running=False, done=True, error=str(e))


def _resolve_workspace_dir(job_name):
    """兼容新旧两种工作区命名，使用绝对路径（避免 cwd 变化导致找不到目录）"""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, f"工作区_{safe_filename(job_name)}", "简历原始文件"),
        os.path.join(base, f"工作区_{job_name}", "简历原始文件"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def _parse_html_filename(fname):
    """从 '001_姓名_resumeId.html[.gz]' 解析 (resume_id, name_hint)"""
    stem = fname
    for sfx in (".html.gz", ".html"):
        if stem.endswith(sfx):
            stem = stem[:-len(sfx)]
            break
    parts = stem.split("_")
    rid = parts[-1] if parts else stem
    name_hint = parts[1] if len(parts) >= 3 else (parts[0] if parts else "未知")
    return rid, name_hint


def run_evaluation_for_ids(job_name, resume_ids, force_reevaluate=False):
    """批次评估：只处理 resume_ids 集合中的简历，其余跳过。
    force_reevaluate=True 时先删除该批次的旧 LLM 评估记录（保留 prefilter_rejects）。
    """
    session_id = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    resume_ids = set(resume_ids)
    _set_state(job_name, running=True, phase="evaluating",
               current=0, total=0, last_msg="开始批次评估",
               done=False, error=None, stop_requested=False)
    try:
        client = llm_utils.initialize_client()
        if not client:
            _set_state(job_name, running=False, done=True,
                       error="LLM 客户端不可用"); return

        job_record = database.get_job(job_name)
        if not job_record:
            _set_state(job_name, running=False, done=True,
                       error=f"找不到岗位 {job_name}"); return

        job_id = job_record["id"]
        html_dir = _resolve_workspace_dir(job_name)
        if not html_dir or not os.path.exists(html_dir):
            _set_state(job_name, running=False, done=True,
                       error="找不到简历目录"); return

        # 找到批次中对应的 HTML 文件
        batch_files = []
        for fname in sorted(f for f in os.listdir(html_dir)
                            if f.endswith(".html") or f.endswith(".html.gz")):
            rid, _ = _parse_html_filename(fname)
            if rid in resume_ids:
                batch_files.append((fname, rid))

        if not batch_files:
            _log(job_name, "批次简历文件均未找到（可能已被删除）")
            _set_state(job_name, running=False, done=True, phase="done"); return

        # 强制重评：清除该批次的旧 LLM 评估（不动 prefilter_rejects）
        if force_reevaluate:
            deleted = 0
            with database.get_conn() as conn:
                for _, rid in batch_files:
                    row = conn.execute(
                        "SELECT id FROM candidates WHERE resume_id=?", (rid,)).fetchone()
                    if not row: continue
                    ev = conn.execute(
                        "SELECT id FROM evaluations WHERE job_id=? AND candidate_id=?",
                        (job_id, row["id"])).fetchone()
                    if not ev: continue
                    eid = ev["id"]
                    conn.execute("DELETE FROM outcomes WHERE evaluation_id=?", (eid,))
                    conn.execute("DELETE FROM rejection_tags WHERE evaluation_id=?", (eid,))
                    conn.execute("DELETE FROM evaluations WHERE id=?", (eid,))
                    deleted += 1
            if deleted:
                _log(job_name, f"批次重评：已清空 {deleted} 条旧记录")

        already_done = (database.get_evaluated_resume_ids(job_id)
                        | database.get_prefilter_reject_resume_ids(job_id))
        target = [(f, r) for f, r in batch_files if r not in already_done]
        _set_state(job_name, skip_count=len(batch_files) - len(target))

        total = len(target)
        _log(job_name, f"批次{'重评' if force_reevaluate else '评估'}：{total} 份")
        if total == 0:
            _log(job_name, "✓ 批次中所有简历均已处理")
            _set_state(job_name, running=False, done=True, phase="done"); return

        _set_state(job_name, total=total, current=0)
        _log(job_name,
             f"并发{'重评' if force_reevaluate else '评估'}（{MAX_EVAL_WORKERS} 线程），共 {total} 份")

        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS,
                                thread_name_prefix=f"batch-{job_name[:8]}") as ex:
            futs = {}
            for fname, resume_id in target:
                _, name_hint = _parse_html_filename(fname)
                fut = ex.submit(process_resume, client,
                                os.path.join(html_dir, fname),
                                name_hint, resume_id, job_record,
                                session_id=session_id)
                futs[fut] = name_hint
            for fut in as_completed(futs):
                if is_stop_requested(job_name):
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                name_hint = futs[fut]
                try:
                    _, verdict = fut.result()
                except Exception as e:
                    verdict = None
                    _log(job_name, f"  ⚠ 异常 {name_hint}: {e}")
                    logger.exception(e)
                completed += 1
                _set_state(job_name, current=completed)
                if verdict == "排除": _log(job_name, f"  ({completed}/{total}) {name_hint} → 排除")
                elif verdict:         _log(job_name, f"  ({completed}/{total}) {name_hint} → {verdict}")
                else:                 _log(job_name, f"  ({completed}/{total}) {name_hint} ✗ 处理失败")

        if is_stop_requested(job_name):
            _set_state(job_name, running=False, done=True,
                       phase="done", last_msg="已停止")
        else:
            _log(job_name, f"✓ 批次完成（{completed} 份）")
            _set_state(job_name, running=False, done=True, phase="done")
    except Exception as e:
        logger.exception(e)
        _set_state(job_name, running=False, done=True, error=str(e))


def run_evaluation_for_ids_in_thread(job_name, resume_ids, force_reevaluate=False):
    t = threading.Thread(
        target=run_evaluation_for_ids,
        args=(job_name, set(resume_ids), force_reevaluate),
        daemon=True, name=f"batch-{job_name[:12]}")
    t.start()
    return t


def run_evaluation_in_thread(job_name, force_reevaluate=False):
    t = threading.Thread(
        target=run_evaluation,
        args=(job_name, force_reevaluate),
        daemon=True,
    )
    t.start()
    return t


# =============================================================================
# Prompt 沙盒测试
# =============================================================================

def sandbox_test(job_name, prompt_draft, sample_size=5):
    """
    用草稿 Prompt 在该岗位现有候选人中随机抽样评估，结果不写库。
    返回 list[{name, verdict, 符合, 不符合, verdict_reason}]
    """
    import random
    client = llm_utils.initialize_client()
    if not client:
        return {"ok": False, "message": "LLM 客户端不可用"}

    job_record = database.get_job(job_name)
    if not job_record:
        return {"ok": False, "message": f"岗位 {job_name} 不存在"}

    # 取该岗位已有评估候选人作为样本
    with database.get_conn() as conn:
        rows = conn.execute("""
            SELECT c.name, c.structured_json
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            WHERE e.job_id=? AND c.structured_json IS NOT NULL
                AND e.verdict != '排除'
            ORDER BY RANDOM()
            LIMIT ?
        """, (job_record["id"], sample_size)).fetchall()
        rows = [dict(r) for r in rows]

    if not rows:
        return {"ok": False,
                "message": "该岗位暂无可用样本，请先正式评估几份简历"}

    results = []
    for r in rows:
        try:
            structured = json.loads(r["structured_json"])
        except Exception:
            continue
        try:
            judgment = judger.judge(client, structured, prompt_draft, job_name)
            if judgment:
                results.append({
                    "name": r["name"],
                    "verdict": judgment.get("verdict"),
                    "符合": judgment.get("符合", []),
                    "不符合": judgment.get("不符合", []),
                    "verdict_reason": judgment.get("verdict_reason", ""),
                })
        except Exception as e:
            results.append({
                "name": r["name"],
                "verdict": "ERROR",
                "符合": [],
                "不符合": [{"条件": "评估失败", "原因": str(e)[:60]}],
                "verdict_reason": "",
            })
    return {"ok": True, "results": results}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="岗位名称")
    parser.add_argument("--reeval", action="store_true", help="清空旧评估后重跑")
    args = parser.parse_args()
    run_evaluation(args.name, force_reevaluate=args.reeval)
