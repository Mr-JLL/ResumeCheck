"""
评估守护进程 (worker.py)
========================
随 app.py 自动启动，后台持续评估所有岗位待处理简历。
- 状态真相来源：磁盘文件夹 + 数据库，重启不丢失
- 3 线程并发评估，速度约为串行的 3 倍，总 token 不变
- 支持按岗位暂停/恢复
"""

import os
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import database
import pipeline
import llm_utils

logger = logging.getLogger(__name__)

MAX_WORKERS = 3      # 并发线程数（对应 DeepSeek 并发请求）
SCAN_INTERVAL = 12   # 秒：每轮扫描间隔

_paused = set()
_paused_lock = threading.Lock()
_running = False
_thread = None


# ── 暂停 / 恢复 ────────────────────────────────────────

def pause_job(job_name: str):
    with _paused_lock:
        _paused.add(job_name)
    pipeline.request_stop(job_name)   # 令正在跑的评估尽快退出


def resume_job(job_name: str):
    with _paused_lock:
        _paused.discard(job_name)


def is_paused(job_name: str) -> bool:
    with _paused_lock:
        return job_name in _paused


# ── 真实状态（DB + 磁盘派生）────────────────────────────

def get_job_status(job_name: str) -> dict:
    """
    不依赖内存 _state，任何时候都准确。
    关闭网页重开后调用此函数，UI 直接显示正确状态。
    """
    job = database.get_job(job_name)
    if not job:
        return {}
    job_id = job["id"]

    html_dir = pipeline._resolve_workspace_dir(job_name)
    html_total = 0
    if html_dir and os.path.exists(html_dir):
        html_total = sum(
            1 for f in os.listdir(html_dir)
            if f.endswith(".html") or f.endswith(".html.gz")
        )

    evaluated = len(database.get_evaluated_resume_ids(job_id))
    prefilter_rej = database.count_prefilter_rejects(job_id)
    pending = max(0, html_total - evaluated - prefilter_rej)

    mem = pipeline.get_state(job_name)
    return {
        "html_total":        html_total,
        "evaluated":         evaluated,
        "prefilter_rejected": prefilter_rej,
        "pending":           pending,
        "is_running":        mem.get("running", False),
        "is_paused":         is_paused(job_name),
        "phase":             mem.get("phase", "idle"),
        "current":           mem.get("current", 0),
        "total":             mem.get("total", 0),
        "last_msg":          mem.get("last_msg", ""),
        "error":             mem.get("error"),
    }


# ── 并发评估单个岗位 ────────────────────────────────────

def _eval_one(client, html_path: str, name_hint: str,
              resume_id: str, job_record: dict):
    try:
        return pipeline.process_resume(client, html_path, name_hint,
                                       resume_id, job_record)
    except Exception as e:
        logger.exception(f"评估异常 {name_hint}: {e}")
        return None, None


def _run_job(job_name: str):
    """并发评估一个岗位所有待处理简历（在独立线程中运行）。"""
    pipeline._set_state(job_name, running=True, phase="evaluating",
                        current=0, total=0, done=False,
                        error=None, stop_requested=False)
    try:
        client = llm_utils.initialize_client()
        if not client:
            pipeline._set_state(job_name, running=False, done=True,
                                error="LLM 客户端不可用：请检查 .env 中的 DEEPSEEK_API_KEY")
            return

        job_record = database.get_job(job_name)
        if not job_record:
            pipeline._set_state(job_name, running=False, done=True,
                                error=f"找不到岗位 {job_name}")
            return

        job_id = job_record["id"]
        html_dir = pipeline._resolve_workspace_dir(job_name)
        if not html_dir or not os.path.exists(html_dir):
            pipeline._set_state(job_name, running=False, done=True)
            return

        all_files = sorted(
            f for f in os.listdir(html_dir)
            if f.endswith(".html") or f.endswith(".html.gz")
        )
        done_ids = (database.get_evaluated_resume_ids(job_id)
                    | database.get_prefilter_reject_resume_ids(job_id))

        # 收集待处理文件
        tasks = []   # (html_path, name_hint, resume_id)
        for fname in all_files:
            rid, hint = pipeline._parse_html_filename(fname)
            if rid not in done_ids:
                tasks.append((os.path.join(html_dir, fname), hint, rid))

        total = len(tasks)
        if total == 0:
            pipeline._log(job_name, "✓ 无新简历待评估")
            pipeline._set_state(job_name, running=False, done=True, phase="done")
            return

        pipeline._log(job_name,
                      f"开始并发评估（{MAX_WORKERS} 线程），共 {total} 份")
        pipeline._set_state(job_name, total=total, current=0)

        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {
                ex.submit(_eval_one, client, hp, hint, rid, job_record): hint
                for hp, hint, rid in tasks
            }
            for fut in as_completed(futs):
                if pipeline.is_stop_requested(job_name) or is_paused(job_name):
                    # 取消剩余 future（Python 3.9+ cancel_futures 参数）
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                _, verdict = fut.result()
                completed += 1
                pipeline._set_state(job_name, current=completed)
                label = verdict if verdict else "✗ 失败"
                pipeline._log(job_name,
                              f"  ({completed}/{total}) {futs[fut]} → {label}")

        if pipeline.is_stop_requested(job_name) or is_paused(job_name):
            pipeline._set_state(job_name, running=False, done=True,
                                phase="done", last_msg="已暂停")
        else:
            pipeline._log(job_name, f"✓ 评估完成（共 {completed} 份）")
            pipeline._set_state(job_name, running=False, done=True, phase="done")

    except Exception as e:
        logger.exception(e)
        pipeline._set_state(job_name, running=False, done=True, error=str(e))


# ── 守护主循环 ──────────────────────────────────────────

def _loop():
    logger.info("[worker] 评估守护进程已启动")
    while _running:
        try:
            for job in database.list_jobs():
                name = job["name"]
                if not _running:
                    break
                if is_paused(name):
                    continue
                if pipeline.get_state(name).get("running"):
                    continue   # 该岗位已在评估中
                if get_job_status(name).get("pending", 0) > 0:
                    logger.info(f"[worker] {name}: 发现待评估简历，启动评估线程")
                    t = threading.Thread(target=_run_job, args=(name,),
                                         daemon=True, name=f"eval-{name[:12]}")
                    t.start()
                    time.sleep(1)   # 错开启动，降低 DB 写锁争用
        except Exception as e:
            logger.exception(f"[worker] 扫描异常: {e}")
        time.sleep(SCAN_INTERVAL)


def start():
    global _thread, _running
    if _thread and _thread.is_alive():
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True, name="eval-worker")
    _thread.start()


def stop():
    global _running
    _running = False
