"""
学习系统 v2
==========
阶段1：outcome 日志统计
阶段3：特征-结果相关性分析
阶段4：招聘漏斗健康度看板（每岗位漏斗+预警）
阶段5：市场稀缺性分析（哪个条件最难满足）
阶段6：反向 JD 优化器（≥100 评估时启用）
阶段7：淘汰原因标签统计 + 高频规则建议
"""

import sys
import json
import time
import logging
import collections
import datetime
import threading

import database

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)


# 5 分钟缓存
_CACHE = {"data": None, "timestamp": 0}
_CACHE_TTL = 300
_CACHE_LOCK = threading.Lock()


def invalidate_cache():
    with _CACHE_LOCK:
        _CACHE["data"] = None
        _CACHE["timestamp"] = 0


ACTION_LABELS = {
    "contacted": "已联系",
    "interviewed": "已面试",
    "hired": "已录用",
    "skipped": "跳过",
    "rejected": "已淘汰",
    "approved": "通过",
    "disapproved": "不通过",
    "note_only": "仅备注",
    None: "无操作",
}


def _now():
    return datetime.datetime.now()


def _iso_days_ago(days):
    return (_now() - datetime.timedelta(days=days)).isoformat(timespec='seconds')


# =============================================================================
# 阶段1：操作分布统计
# =============================================================================

def stage1_outcome_summary(job_name=None):
    rows = database.get_outcome_summary(job_name)
    out = []
    for r in rows:
        out.append({
            "verdict": r["verdict"],
            "action": r["action"],
            "action_label": ACTION_LABELS.get(r["action"], r["action"] or "无操作"),
            "cnt": r["cnt"],
        })
    verdict_order = {"深绿": 0, "蓝色": 1, "黄色": 2, "排除": 3}
    out.sort(key=lambda x: (verdict_order.get(x["verdict"], 99), -x["cnt"]))
    return out


# =============================================================================
# 阶段3：特征-结果相关性分析
# =============================================================================

def _safe_loads(s):
    try:
        return json.loads(s) if s else None
    except Exception:
        return None


def stage3_correlation():
    with database.get_conn() as conn:
        rows = conn.execute("""
            SELECT c.first_degree, c.school, c.english_level, c.total_years,
                   c.structured_json,
                   e.verdict,
                   (SELECT action FROM outcomes WHERE evaluation_id=e.id
                    ORDER BY action_at DESC LIMIT 1) AS final_action
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
        """).fetchall()
        rows = [dict(r) for r in rows]

    total = len(rows)
    if total < 200:
        return None

    def _is_positive(action):
        return action in ("hired", "approved")

    degree_stats = collections.defaultdict(lambda: {"total": 0, "hired": 0})
    for r in rows:
        d = r.get("first_degree") or "未明确"
        degree_stats[d]["total"] += 1
        if _is_positive(r.get("final_action")):
            degree_stats[d]["hired"] += 1

    eng_stats = collections.defaultdict(lambda: {"total": 0, "hired": 0})
    for r in rows:
        e = r.get("english_level") or "未提及"
        eng_stats[e]["total"] += 1
        if _is_positive(r.get("final_action")):
            eng_stats[e]["hired"] += 1

    years_stats = collections.defaultdict(lambda: {"total": 0, "hired": 0})
    for r in rows:
        y = r.get("total_years")
        if y is None:
            bucket = "未知"
        elif y < 3:
            bucket = "<3年"
        elif y < 5:
            bucket = "3-5年"
        elif y < 10:
            bucket = "5-10年"
        else:
            bucket = "≥10年"
        years_stats[bucket]["total"] += 1
        if _is_positive(r.get("final_action")):
            years_stats[bucket]["hired"] += 1

    industry_stats = collections.defaultdict(lambda: {"total": 0, "hired": 0})
    for r in rows:
        sj = _safe_loads(r.get("structured_json"))
        if not sj:
            continue
        tags = sj.get("industry_tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        for t in tags:
            industry_stats[str(t)]["total"] += 1
            if _is_positive(r.get("final_action")):
                industry_stats[str(t)]["hired"] += 1

    return {
        "total_evaluations": total,
        "by_first_degree": _format_stats_dict(degree_stats),
        "by_english_level": _format_stats_dict(eng_stats),
        "by_work_years": _format_stats_dict(years_stats),
        "by_industry_tag": _format_stats_dict(industry_stats),
    }


def _format_stats_dict(d):
    out = []
    for key, v in d.items():
        rate = (v["hired"] / v["total"] * 100) if v["total"] > 0 else 0
        out.append({
            "key": key,
            "total": v["total"],
            "hired": v["hired"],
            "hire_rate": f"{rate:.1f}%",
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def render_correlation_html(corr):
    if not corr:
        return ""
    parts = [f"<p>共 {corr['total_evaluations']} 条评估记录。</p>"]
    sections = [
        ("按第一学历", corr["by_first_degree"]),
        ("按英语水平", corr["by_english_level"]),
        ("按工作年限", corr["by_work_years"]),
        ("按行业标签 Top10", corr["by_industry_tag"][:10]),
    ]
    for title, items in sections:
        parts.append(f"<h3>{title}</h3>")
        parts.append("<table class='job-table'><thead><tr>"
                     "<th>类别</th><th>总数</th><th>录用数</th><th>录用率</th>"
                     "</tr></thead><tbody>")
        for it in items:
            parts.append(f"<tr><td>{it['key']}</td>"
                         f"<td>{it['total']}</td>"
                         f"<td>{it['hired']}</td>"
                         f"<td>{it['hire_rate']}</td></tr>")
        parts.append("</tbody></table>")
    return "\n".join(parts)


# =============================================================================
# 阶段4：招聘漏斗健康度看板
# =============================================================================

def stage4_funnel_overview():
    """
    每个岗位返回一行漏斗指标 + 状态预警
    返回 list[{job_id, name, week_new, deepgreen_rate, prefilter_pass_rate,
              approved_rate, status, alerts}]
    """
    week_ago = _iso_days_ago(7)
    fortnight_ago = _iso_days_ago(14)

    jobs = database.list_jobs()
    out = []
    for j in jobs:
        job_id = j["id"]

        # 本周新增评估
        week_new = database.count_evaluations_for_job(job_id, since_iso=week_ago)
        total_eval = database.count_evaluations_for_job(job_id)

        # 本周 verdict 分布
        verdicts_week = database.count_verdicts_for_job(job_id, since_iso=week_ago)
        verdicts_total = database.count_verdicts_for_job(job_id)

        # 预筛拒绝数
        prefilter_rejects = database.count_prefilter_rejects(job_id)

        # 预筛通过率
        total_attempts = total_eval + prefilter_rejects
        prefilter_pass_rate = (
            (total_eval / total_attempts * 100) if total_attempts > 0 else 0)

        # 深绿率（基于本周）
        if week_new > 0:
            deepgreen_rate = (verdicts_week.get("深绿", 0) / week_new * 100)
        else:
            deepgreen_rate = 0

        # 全周期深绿率（用于"无新数据"时降级显示）
        if total_eval > 0:
            deepgreen_rate_total = (
                verdicts_total.get("深绿", 0) / total_eval * 100)
        else:
            deepgreen_rate_total = 0

        # 操作转化（本周）
        actions_week = database.get_action_counts_for_job(
            job_id,
            actions=("approved", "hired", "interviewed", "contacted", "rejected", "disapproved"),
            since_iso=week_ago,
        )
        approved_count = actions_week.get("approved", 0) + actions_week.get("hired", 0)
        approved_rate = (
            (approved_count / week_new * 100) if week_new > 0 else 0)

        # 最近评估时间
        last_at = database.get_last_eval_time_for_job(job_id)
        days_since = None
        if last_at:
            try:
                last_dt = datetime.datetime.fromisoformat(last_at)
                days_since = (datetime.datetime.now() - last_dt).days
            except Exception:
                days_since = None

        # 14 天内深绿率（用于连续 2 周判断）
        verdicts_2w = database.count_verdicts_for_job(job_id, since_iso=fortnight_ago)
        total_2w = sum(verdicts_2w.values())
        deepgreen_rate_2w = (
            (verdicts_2w.get("深绿", 0) / total_2w * 100) if total_2w > 0 else 0)

        # 状态预警
        alerts = []
        status = "🟢"

        if days_since is not None and days_since >= 14:
            alerts.append(f"已 {days_since} 天无新评估")
            status = "🔴"
        elif week_new < 10 and total_eval > 0:
            alerts.append(f"本周仅 {week_new} 份新简历，候选人量不足")
            if status != "🔴":
                status = "🟡"

        if total_2w >= 10 and deepgreen_rate_2w < 5:
            alerts.append(f"近两周深绿率仅 {deepgreen_rate_2w:.1f}%，候选人质量持续偏低")
            status = "🔴"

        if total_attempts >= 20:
            if prefilter_pass_rate < 20:
                alerts.append(
                    f"预筛通过率仅 {prefilter_pass_rate:.0f}%，JD 要求可能脱离市场")
                if status != "🔴":
                    status = "🟡"
            elif prefilter_pass_rate > 80:
                alerts.append(
                    f"预筛通过率 {prefilter_pass_rate:.0f}%，硬规则可能过松")
                if status != "🔴":
                    status = "🟡"

        out.append({
            "job_id": job_id,
            "name": j["name"],
            "total_eval": total_eval,
            "week_new": week_new,
            "deepgreen_rate_week": round(deepgreen_rate, 1),
            "deepgreen_rate_total": round(deepgreen_rate_total, 1),
            "deepgreen_count_total": verdicts_total.get("深绿", 0),
            "lightgreen_count_total": verdicts_total.get("蓝色", 0),
            "yellow_count_total": verdicts_total.get("黄色", 0),
            "prefilter_rejects": prefilter_rejects,
            "prefilter_pass_rate": round(prefilter_pass_rate, 1),
            "approved_rate": round(approved_rate, 1),
            "approved_count": approved_count,
            "days_since": days_since,
            "last_at": last_at,
            "status": status,
            "alerts": alerts,
        })
    return out


def stage4_funnel_detail(job_name):
    """单岗位的五阶段漏斗详细数据"""
    job = database.get_job(job_name)
    if not job:
        return None
    job_id = job["id"]

    total_eval = database.count_evaluations_for_job(job_id)
    prefilter_rejects = database.count_prefilter_rejects(job_id)
    scraped = total_eval + prefilter_rejects

    verdicts = database.count_verdicts_for_job(job_id)
    deepgreen = verdicts.get("深绿", 0)
    lightgreen = verdicts.get("蓝色", 0)
    yellow = verdicts.get("黄色", 0)

    actions = database.get_action_counts_for_job(
        job_id,
        actions=("approved", "hired", "disapproved", "rejected", "contacted",
                 "interviewed"))
    approved = actions.get("approved", 0) + actions.get("hired", 0)
    rejected = actions.get("disapproved", 0) + actions.get("rejected", 0)

    stages = [
        {"name": "已抓取", "count": scraped},
        {"name": "通过预筛", "count": total_eval},
        {"name": "深绿", "count": deepgreen},
        {"name": "深绿+蓝色", "count": deepgreen + lightgreen},
        {"name": "HR 通过", "count": approved},
    ]
    # 计算每阶段流失率
    for i, s in enumerate(stages):
        if i == 0:
            s["dropoff_pct"] = 0
        else:
            prev = stages[i - 1]["count"]
            cur = s["count"]
            s["dropoff_pct"] = round(
                (1 - cur / prev) * 100, 1) if prev > 0 else 0
    return {
        "job_name": job_name,
        "stages": stages,
        "rejected": rejected,
        "yellow": yellow,
    }


# =============================================================================
# 阶段5：市场稀缺性分析
# =============================================================================

def stage5_rarity_for_job(job_name, sample_floor=20):
    """
    分析该岗位 mismatches_json 中条件出现频率，
    返回最难满足的若干条件 + 调整建议
    """
    with database.get_conn() as conn:
        rows = conn.execute("""
            SELECT e.mismatches_json, e.cons_json, e.verdict
            FROM evaluations e
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ?
        """, (job_name,)).fetchall()
        rows = [dict(r) for r in rows]

    total = len(rows)
    if total < sample_floor:
        return None

    counter = collections.Counter()
    for r in rows:
        items = _safe_loads(r.get("mismatches_json"))
        if items and isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    cond = (it.get("条件") or "").strip()
                    if cond:
                        counter[cond] += 1
        else:
            # 兼容旧 cons
            cons = _safe_loads(r.get("cons_json"))
            if cons and isinstance(cons, list):
                for c in cons:
                    s = str(c)[:30]
                    if s:
                        counter[s] += 1

    items = []
    for cond, cnt in counter.most_common(10):
        rate = cnt / total * 100
        items.append({
            "condition": cond,
            "count": cnt,
            "miss_rate": round(rate, 1),
            "suggestion": _suggestion_for(cond, rate),
        })
    return {
        "total_evaluations": total,
        "items": items,
    }


def _suggestion_for(cond, miss_rate):
    if miss_rate >= 70:
        return "市场极少满足，建议从硬性要求下调为加分项"
    if miss_rate >= 50:
        return "约半数候选人不满足，可考虑放宽或扩大搜索关键词"
    if miss_rate >= 30:
        return "1/3候选人受阻，可关注此条件或调整描述"
    return "影响较小，保持现状"


# =============================================================================
# 阶段6：反向 JD 优化器
# =============================================================================

def stage6_reverse_jd_optimizer(job_name):
    """≥100 条评估时启用。综合分析 JD 与市场的差异"""
    with database.get_conn() as conn:
        cnt = conn.execute("""
            SELECT COUNT(*) AS c FROM evaluations e
            JOIN jobs j ON e.job_id = j.id WHERE j.name = ?
        """, (job_name,)).fetchone()["c"]
    if cnt < 100:
        return {"available": False, "evaluations": cnt,
                "message": f"该岗位评估数 {cnt} 条，需 ≥100 启用"}

    job = database.get_job(job_name)
    job_id = job["id"]
    job_config = json.loads(job.get("config_json") or "{}")

    # 1. prefilter 淘汰原因分布
    with database.get_conn() as conn:
        rows = conn.execute("""
            SELECT fail_reason FROM prefilter_rejects WHERE job_id=?
        """, (job_id,)).fetchall()
    prefilter_reasons = collections.Counter()
    for r in rows:
        for piece in (r["fail_reason"] or "").split(";"):
            piece = piece.strip()
            if piece:
                prefilter_reasons[piece] += 1
    prefilter_total = sum(prefilter_reasons.values())

    # 2. 深绿候选人共性特征
    with database.get_conn() as conn:
        deep_rows = conn.execute("""
            SELECT c.first_degree, c.english_level, c.school, c.structured_json
            FROM evaluations e
            JOIN candidates c ON e.candidate_id = c.id
            JOIN jobs j ON e.job_id = j.id
            WHERE j.name = ? AND e.verdict = '深绿'
        """, (job_name,)).fetchall()
        deep_rows = [dict(r) for r in deep_rows]

    deep_industries = collections.Counter()
    for r in deep_rows:
        sj = _safe_loads(r.get("structured_json"))
        if sj and isinstance(sj.get("industry_tags"), list):
            for t in sj["industry_tags"]:
                deep_industries[str(t)] += 1

    # 3. 关键稀缺条件
    rarity = stage5_rarity_for_job(job_name, sample_floor=10)

    findings = []

    # findings 1: prefilter 高淘汰原因
    if prefilter_total > 0:
        for reason, cnt in prefilter_reasons.most_common(3):
            pct = cnt / prefilter_total * 100
            findings.append({
                "type": "硬规则",
                "title": f"硬规则淘汰：{reason}",
                "detail": f"共 {cnt} 人因此被预筛淘汰，占预筛淘汰总数 {pct:.0f}%",
                "suggestion": (
                    "若市场上同时满足该条件与其他要求的候选人极少，"
                    "建议放宽该项或将其改为加分项。") if pct > 30 else
                    "保持该规则。",
            })

    # findings 2: 深绿候选人来源行业
    if deep_industries and len(deep_rows) >= 3:
        top = deep_industries.most_common(3)
        ind_str = "、".join(f"{k}({v})" for k, v in top)
        findings.append({
            "type": "深绿画像",
            "title": "深绿候选人主要来自这些行业",
            "detail": ind_str,
            "suggestion": "建议在搜索条件里加入这些行业关键词，提升新批次质量。",
        })

    # findings 3: 稀缺条件
    if rarity and rarity["items"]:
        for it in rarity["items"][:3]:
            if it["miss_rate"] >= 40:
                findings.append({
                    "type": "市场稀缺",
                    "title": f"难以满足：{it['condition']}",
                    "detail": f"{it['miss_rate']}% 的候选人不满足（{it['count']}/{rarity['total_evaluations']}）",
                    "suggestion": it["suggestion"],
                })

    return {
        "available": True,
        "evaluations": cnt,
        "deep_count": len(deep_rows),
        "findings": findings,
    }


# =============================================================================
# 阶段7：淘汰原因标签统计 + 高频规则建议
# =============================================================================

def stage7_rejection_tags(threshold=10):
    """
    返回 list[{tag_text, count, suggested}]
    suggested=True 当 count >= threshold 时提示 HR 升级为硬规则
    """
    stats = database.list_rejection_tag_stats(job_id=None, threshold=1)
    out = []
    for s in stats:
        out.append({
            "tag_text": s["tag_text"],
            "count": s["count"],
            "last_at": s["last_at"],
            "suggested": s["count"] >= threshold,
        })
    return out


# =============================================================================
# 综合页面数据
# =============================================================================

def get_learning_page_data():
    """供 /learning 路由调用，带 5 分钟缓存"""
    with _CACHE_LOCK:
        if _CACHE["data"] and (time.time() - _CACHE["timestamp"] < _CACHE_TTL):
            return _CACHE["data"]

    outcome_rows = stage1_outcome_summary()
    corr = stage3_correlation()
    correlation_html = render_correlation_html(corr) if corr else None
    rejection_tags = stage7_rejection_tags()

    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM evaluations").fetchone()["c"]

    data = {
        "outcome_rows": outcome_rows,
        "correlation": correlation_html,
        "total_samples": total,
        "rejection_tags": rejection_tags,
    }
    with _CACHE_LOCK:
        _CACHE["data"] = data
        _CACHE["timestamp"] = time.time()
    return data


def get_funnel_page_data():
    """供 /funnel 路由调用"""
    return {
        "rows": stage4_funnel_overview(),
        "now": _now().strftime("%Y-%m-%d %H:%M"),
    }


# =============================================================================
# 诊断报告：AI 校准偏差检测
# =============================================================================

def audit_calibration_bias(job_id=None):
    """
    分析 HR 操作与 AI 判断的一致性。
    override_approve = HR 通过了 AI 黄色/排除
    override_reject  = HR 拒绝了 AI 深绿
    返回：{override_approve_rate, override_reject_rate, severity, details, total_handled}
    """
    rows = database.get_calibration_stats(job_id)

    bucket = {}
    for r in rows:
        v = r["verdict"]
        a = r["action"]
        bucket.setdefault(v, {})
        bucket[v][a] = r["cnt"]

    def _pos(d):
        return d.get("approved", 0) + d.get("hired", 0)

    def _neg(d):
        return d.get("disapproved", 0) + d.get("rejected", 0)

    def _total(d):
        return sum(d.values())

    total_handled = sum(_total(d) for d in bucket.values())

    deep_green = bucket.get("深绿", {})
    yellow = bucket.get("黄色", {})
    excluded = bucket.get("排除", {})

    dg_total = _total(deep_green)
    dg_rejected = _neg(deep_green)
    dg_reject_rate = (dg_rejected / dg_total * 100) if dg_total > 0 else 0.0

    yw_total = _total(yellow)
    yw_approved = _pos(yellow)
    yw_approve_rate = (yw_approved / yw_total * 100) if yw_total > 0 else 0.0

    ex_total = _total(excluded)
    ex_approved = _pos(excluded)
    ex_approve_rate = (ex_approved / ex_total * 100) if ex_total > 0 else 0.0

    severity = "green"
    findings = []

    if dg_reject_rate > 25:
        severity = "red"
        findings.append({
            "level": "严重",
            "text": f"深绿候选人被 HR 拒绝率高达 {dg_reject_rate:.0f}%（共 {dg_total} 人处理）",
            "suggest": "AI 评分标准可能过于宽松，建议审查 prompt 中的硬性条件定义",
        })
    elif dg_reject_rate > 12:
        severity = "yellow" if severity == "green" else severity
        findings.append({
            "level": "警告",
            "text": f"深绿候选人被 HR 拒绝率 {dg_reject_rate:.0f}%（共 {dg_total} 人处理）",
            "suggest": "存在一定偏差，建议观察近期被拒深绿的共同特征",
        })

    if yw_approve_rate > 35:
        severity = "yellow" if severity == "green" else severity
        findings.append({
            "level": "提示",
            "text": f"黄色候选人被 HR 通过率 {yw_approve_rate:.0f}%（共 {yw_total} 人处理）",
            "suggest": "AI 评分偏保守，许多黄色候选人实际符合要求，可考虑放宽软性条件阈值",
        })

    if ex_approved > 0:
        severity = "red"
        findings.append({
            "level": "严重",
            "text": f"「排除」候选人被 HR 通过 {ex_approved} 人（占排除处理量 {ex_approve_rate:.0f}%）",
            "suggest": "预筛硬性条件可能过于严格，或 LLM 误判了部分候选人的硬性条件",
        })

    if not findings:
        findings.append({
            "level": "正常",
            "text": f"AI 与 HR 判断高度一致（共处理 {total_handled} 人）",
            "suggest": "当前校准状态良好，无需调整",
        })

    score = max(0.0, 1.0 - dg_reject_rate / 50 - ex_approve_rate / 100)

    return {
        "total_handled": total_handled,
        "dg_total": dg_total,
        "dg_reject_rate": round(dg_reject_rate, 1),
        "yw_total": yw_total,
        "yw_approve_rate": round(yw_approve_rate, 1),
        "ex_total": ex_total,
        "ex_approve_rate": round(ex_approve_rate, 1),
        "severity": severity,
        "findings": findings,
        "score": round(score, 3),
    }


# =============================================================================
# 诊断报告：预筛漏斗异常告警
# =============================================================================

def audit_prefilter_anomaly(job_id, job_name):
    """
    比较各抓取批次的评估数量趋势，识别异常批次。
    同时分析当前预筛通过率是否在健康区间（25%-75%）。
    """
    with database.get_conn() as conn:
        sessions = conn.execute("""
            SELECT e.scrape_session_id,
                   COUNT(*) AS eval_count,
                   MIN(e.evaluated_at) AS session_start
            FROM evaluations e
            WHERE e.job_id=? AND e.scrape_session_id IS NOT NULL
            GROUP BY e.scrape_session_id
            ORDER BY session_start DESC LIMIT 10
        """, (job_id,)).fetchall()
        sessions = [dict(s) for s in sessions]

    total_eval = database.count_total_evaluations_for_job(job_id)
    pf_rejects = database.count_prefilter_rejects(job_id)
    total_attempts = total_eval + pf_rejects
    pass_rate = (total_eval / total_attempts * 100) if total_attempts > 0 else 0.0

    findings = []
    severity = "green"

    if total_attempts >= 20:
        if pass_rate < 20:
            severity = "red"
            findings.append({
                "level": "严重",
                "text": f"预筛通过率仅 {pass_rate:.0f}%（{total_eval}/{total_attempts}），低于健康下限 20%",
                "suggest": "硬性条件可能脱离市场现状，建议查看供需矩阵并考虑放宽条件",
            })
        elif pass_rate < 30:
            severity = "yellow"
            findings.append({
                "level": "警告",
                "text": f"预筛通过率 {pass_rate:.0f}%，略低于健康区间",
                "suggest": "可检查是否某一条件导致大量淘汰",
            })
        elif pass_rate > 80:
            severity = "yellow" if severity == "green" else severity
            findings.append({
                "level": "提示",
                "text": f"预筛通过率高达 {pass_rate:.0f}%，硬性条件可能过松",
                "suggest": "考虑收紧预筛硬条件，减少 LLM 无效调用成本",
            })

    # 批次异常检测
    if len(sessions) >= 3:
        counts = [s["eval_count"] for s in sessions]
        avg = sum(counts) / len(counts)
        for s in sessions:
            ratio = s["eval_count"] / avg if avg > 0 else 1
            if ratio < 0.3:
                findings.append({
                    "level": "提示",
                    "text": f"批次 {s['session_start'][:10]} 仅产出 {s['eval_count']} 条，远低于平均 {avg:.0f}",
                    "suggest": "该批次可能抓取中途中断，或当时候选人质量极差",
                })
                severity = "yellow" if severity == "green" else severity

    if not findings:
        findings.append({
            "level": "正常",
            "text": f"预筛通过率 {pass_rate:.0f}%，处于健康区间",
            "suggest": "无异常",
        })

    score = 1.0
    if pass_rate < 20:
        score = 0.2
    elif pass_rate < 30:
        score = 0.6
    elif pass_rate > 80:
        score = 0.7
    else:
        score = 1.0

    return {
        "pass_rate": round(pass_rate, 1),
        "total_eval": total_eval,
        "pf_rejects": pf_rejects,
        "total_attempts": total_attempts,
        "sessions": sessions[:5],
        "findings": findings,
        "severity": severity,
        "score": score,
    }


# =============================================================================
# 诊断报告：条件触发频率分析
# =============================================================================

def audit_condition_trigger_freq(job_id, job_name):
    """
    分析 mismatches_json 中各条件出现频率。
    过热（>60%）= 可能过严；过冷（<5%）= 可能冗余或 LLM 未执行。
    """
    raw_list = database.get_mismatch_conditions_for_job(job_id)
    total_evals = len(raw_list)
    if total_evals < 10:
        return {"available": False, "total_evals": total_evals,
                "message": f"评估数 {total_evals} 不足 10 条"}

    counter = collections.Counter()
    for raw in raw_list:
        try:
            items = json.loads(raw) if raw else []
        except Exception:
            continue
        seen = set()
        for it in (items or []):
            if isinstance(it, dict):
                cond = (it.get("条件") or "").strip()[:30]
                if cond and cond not in seen:
                    counter[cond] += 1
                    seen.add(cond)

    if not counter:
        return {"available": False, "total_evals": total_evals,
                "message": "未找到条件触发数据"}

    items_out = []
    hot_conditions = []
    cold_conditions = []

    for cond, cnt in counter.most_common(15):
        rate = cnt / total_evals * 100
        if rate >= 60:
            status = "hot"
            hot_conditions.append(cond)
        elif rate >= 20:
            status = "warm"
        elif rate >= 5:
            status = "cool"
        else:
            status = "cold"
            cold_conditions.append(cond)
        items_out.append({
            "condition": cond,
            "count": cnt,
            "rate": round(rate, 1),
            "status": status,
        })

    findings = []
    severity = "green"

    if hot_conditions:
        severity = "yellow"
        findings.append({
            "level": "警告",
            "text": f"以下条件在 ≥60% 评估中触发不符合：{' / '.join(hot_conditions[:3])}",
            "suggest": "这些条件正在淘汰大多数候选人，建议评估是否能放宽或转为加分项",
        })

    if cold_conditions:
        findings.append({
            "level": "提示",
            "text": f"以下条件几乎从未触发不符合（<5%）：{' / '.join(cold_conditions[:3])}",
            "suggest": "这些条件可能在市场上普遍满足，或 LLM 未能有效检测违规",
        })

    if not findings:
        findings.append({
            "level": "正常",
            "text": "各条件触发率分布均衡，无明显过热或过冷",
            "suggest": "当前 prompt 条件设置合理",
        })

    score = max(0.0, 1.0 - len(hot_conditions) * 0.2 - len(cold_conditions) * 0.1)

    return {
        "available": True,
        "total_evals": total_evals,
        "items": items_out,
        "hot_conditions": hot_conditions,
        "cold_conditions": cold_conditions,
        "findings": findings,
        "severity": severity,
        "score": round(score, 3),
    }


# =============================================================================
# 诊断报告：证据质量追踪
# =============================================================================

def audit_evidence_quality(job_id):
    """
    分析 matches_json 中的证据字段质量。
    好证据 = 包含原文引用（有「」『』或引号），或含数字/百分比。
    """
    raw_list = database.get_evidence_quality_stats(job_id)
    total_evals = len(raw_list)
    if total_evals < 5:
        return {"available": False, "total_evals": total_evals,
                "message": f"评估数 {total_evals} 不足 5 条"}

    import re
    quote_pat = re.compile(r'[「」『』"""\'\'""''《》【】]')
    num_pat = re.compile(r'\d+[%年月天件台套项]|\d+\.\d+')

    with_evidence = 0
    high_quality = 0
    empty_evidence = 0
    total_match_items = 0

    for raw in raw_list:
        try:
            items = json.loads(raw) if raw else []
        except Exception:
            continue
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            total_match_items += 1
            ev = (it.get("证据") or "").strip()
            if not ev or ev in ("未提及", "无", "无证据"):
                empty_evidence += 1
            else:
                with_evidence += 1
                if quote_pat.search(ev) or num_pat.search(ev):
                    high_quality += 1

    if total_match_items == 0:
        return {"available": False, "total_evals": total_evals,
                "message": "无 matches 数据"}

    empty_rate = empty_evidence / total_match_items * 100
    high_quality_rate = high_quality / total_match_items * 100

    findings = []
    severity = "green"

    if empty_rate > 40:
        severity = "red"
        findings.append({
            "level": "严重",
            "text": f"{empty_rate:.0f}% 的符合条件缺乏具体证据引用",
            "suggest": "LLM 可能在凭印象判断而非原文，建议在 prompt 中强化「必须引用原文」要求",
        })
    elif empty_rate > 20:
        severity = "yellow"
        findings.append({
            "level": "警告",
            "text": f"{empty_rate:.0f}% 的符合条件缺乏具体证据",
            "suggest": "部分判断缺少原文支撑，评估结果可信度存疑",
        })

    if high_quality_rate < 30 and severity == "green":
        severity = "yellow"
        findings.append({
            "level": "提示",
            "text": f"含量化数据或原文引号的高质量证据仅占 {high_quality_rate:.0f}%",
            "suggest": "可在 prompt 中要求：证据必须包含数字、时间或原文引用",
        })
    elif high_quality_rate >= 50:
        findings.append({
            "level": "正常",
            "text": f"{high_quality_rate:.0f}% 的证据含量化数据或原文引用，质量良好",
            "suggest": "继续保持",
        })

    if not findings:
        findings.append({
            "level": "正常",
            "text": f"证据字段填充率 {(with_evidence/total_match_items*100):.0f}%，质量达标",
            "suggest": "无需调整",
        })

    score = max(0.0, 1.0 - empty_rate / 100 * 1.5 + high_quality_rate / 200)

    return {
        "available": True,
        "total_evals": total_evals,
        "total_match_items": total_match_items,
        "empty_evidence": empty_evidence,
        "empty_rate": round(empty_rate, 1),
        "high_quality": high_quality,
        "high_quality_rate": round(high_quality_rate, 1),
        "findings": findings,
        "severity": severity,
        "score": round(min(1.0, score), 3),
    }


# =============================================================================
# 诊断报告：黄色堆积风险
# =============================================================================

def audit_yellow_pile_risk(job_id):
    """
    分析未处理的黄色候选人堆积情况。
    在职候选人等待超过 7 天流失风险高；离职者等待超过 14 天也应关注。
    """
    rows = database.get_yellow_pile_details(job_id)
    now = _now()

    high_risk = []
    medium_risk = []
    low_risk = []

    for r in rows:
        try:
            ev_dt = datetime.datetime.fromisoformat(r["evaluated_at"])
        except Exception:
            ev_dt = now
        days_waiting = (now - ev_dt).days

        try:
            sj = json.loads(r["structured_json"]) if r.get("structured_json") else {}
        except Exception:
            sj = {}

        wh = sj.get("work_history") or []
        is_currently_employed = any(
            isinstance(w, dict) and w.get("is_current") for w in wh
        )

        item = {
            "eval_id": r["eval_id"],
            "name": r["name"],
            "age": r["age"],
            "days_waiting": days_waiting,
            "is_employed": is_currently_employed,
        }

        if is_currently_employed and days_waiting >= 7:
            item["risk_level"] = "high"
            high_risk.append(item)
        elif days_waiting >= 14:
            item["risk_level"] = "medium"
            medium_risk.append(item)
        else:
            item["risk_level"] = "low"
            low_risk.append(item)

    total_yellow = len(rows)
    findings = []
    severity = "green"

    if high_risk:
        severity = "red" if len(high_risk) >= 3 else "yellow"
        findings.append({
            "level": "严重" if len(high_risk) >= 3 else "警告",
            "text": f"{len(high_risk)} 位在职黄色候选人等待超过 7 天，流失风险极高",
            "suggest": "立即优先处理这些候选人，或请相关岗位负责人介入",
        })

    if medium_risk:
        severity = "yellow" if severity == "green" else severity
        findings.append({
            "level": "提示",
            "text": f"{len(medium_risk)} 位候选人等待超过 14 天",
            "suggest": "建议本周内完成处理",
        })

    all_items = high_risk + medium_risk + low_risk
    avg_wait = (sum(item["days_waiting"] for item in all_items) / total_yellow) if total_yellow > 0 else 0

    if total_yellow == 0:
        findings.append({
            "level": "正常",
            "text": "无积压的黄色候选人",
            "suggest": "处理速度良好",
        })
    elif not findings:
        findings.append({
            "level": "正常",
            "text": f"黄色候选人平均等待 {avg_wait:.0f} 天，风险可控",
            "suggest": "保持当前处理节奏",
        })

    score = max(0.0, 1.0 - len(high_risk) * 0.2 - len(medium_risk) * 0.1)

    return {
        "total_yellow": total_yellow,
        "high_risk": high_risk,
        "medium_risk": medium_risk,
        "low_risk_count": len(low_risk),
        "avg_wait_days": round(avg_wait, 1),
        "findings": findings,
        "severity": severity,
        "score": round(score, 3),
    }


# =============================================================================
# 供需敏感性矩阵
# =============================================================================

def supply_demand_matrix(job_name):
    """
    分析预筛硬性条件的"供需缺口"：
    对每个条件，计算"若只有这一条件失败"的候选人数，即放宽该条件的扩池潜力。
    """
    job = database.get_job(job_name)
    if not job:
        return None
    job_id = job["id"]
    job_config = _safe_loads(job.get("config_json")) or {}

    reasons = database.get_prefilter_fail_reasons(job_id)
    if not reasons:
        return {"available": False, "message": "暂无预筛拒绝数据"}

    total_pf = len(reasons)
    total_eval = database.count_total_evaluations_for_job(job_id)
    total_attempts = total_eval + total_pf

    fails = {"age": 0, "edu": 0, "years": 0}
    single_fails = {"age": 0, "edu": 0, "years": 0}

    for r in reasons:
        age_fail = "年龄" in r
        edu_fail = "学历" in r or "第一学历" in r
        yr_fail = "年限" in r

        if age_fail:
            fails["age"] += 1
        if edu_fail:
            fails["edu"] += 1
        if yr_fail:
            fails["years"] += 1

        # "仅该条件失败"
        if age_fail and not edu_fail and not yr_fail:
            single_fails["age"] += 1
        if edu_fail and not age_fail and not yr_fail:
            single_fails["edu"] += 1
        if yr_fail and not age_fail and not edu_fail:
            single_fails["years"] += 1

    conditions = []
    labels = {
        "age": ("年龄上限", job_config.get("max_age") or "未设"),
        "edu": ("学历要求", job_config.get("min_education") or "全日制本科"),
        "years": ("工作年限", str(job_config.get("min_years") or 0) + "年"),
    }

    for key in ("age", "edu", "years"):
        label, current = labels[key]
        fail_cnt = fails[key]
        single = single_fails[key]
        fail_rate = fail_cnt / total_attempts * 100 if total_attempts > 0 else 0
        expand_pct = single / total_attempts * 100 if total_attempts > 0 else 0

        if fail_cnt == 0:
            impact = "none"
        elif fail_rate >= 30:
            impact = "high"
        elif fail_rate >= 15:
            impact = "medium"
        else:
            impact = "low"

        conditions.append({
            "key": key,
            "label": label,
            "current_setting": current,
            "fail_count": fail_cnt,
            "fail_rate": round(fail_rate, 1),
            "single_fail": single,
            "expand_if_relaxed": single,
            "expand_pct": round(expand_pct, 1),
            "impact": impact,
        })

    current_pass_rate = total_eval / total_attempts * 100 if total_attempts > 0 else 0

    return {
        "available": True,
        "total_attempts": total_attempts,
        "total_eval": total_eval,
        "total_pf": total_pf,
        "current_pass_rate": round(current_pass_rate, 1),
        "conditions": conditions,
    }


# =============================================================================
# 综合诊断页面数据
# =============================================================================

def get_audit_page_data(job_name):
    """
    供 /audit/<job_name> 路由调用，聚合 5 个诊断维度。
    使用简单缓存避免频繁计算。
    """
    job = database.get_job(job_name)
    if not job:
        return None
    job_id = job["id"]

    calib = audit_calibration_bias(job_id)
    prefilter_a = audit_prefilter_anomaly(job_id, job_name)
    condition_f = audit_condition_trigger_freq(job_id, job_name)
    evidence_q = audit_evidence_quality(job_id)
    yellow_r = audit_yellow_pile_risk(job_id)
    sdm = supply_demand_matrix(job_name)

    # 综合健康分（0-100）
    scores = [
        calib["score"] * 0.25,
        prefilter_a["score"] * 0.20,
        condition_f.get("score", 0.5) * 0.20,
        evidence_q.get("score", 0.5) * 0.20,
        yellow_r["score"] * 0.15,
    ]
    health_score = round(sum(scores) * 100)

    if health_score >= 80:
        health_level = "良好"
        health_color = "green"
    elif health_score >= 55:
        health_level = "待改善"
        health_color = "yellow"
    else:
        health_level = "需关注"
        health_color = "red"

    return {
        "job_name": job_name,
        "health_score": health_score,
        "health_level": health_level,
        "health_color": health_color,
        "calibration": calib,
        "prefilter": prefilter_a,
        "condition_freq": condition_f,
        "evidence": evidence_q,
        "yellow_pile": yellow_r,
        "supply_demand": sdm,
        "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
    }


# =============================================================================
# 纠错信号分析：生成评估细则草稿
# =============================================================================

def analyze_correction_signals(job_id, job_name, client):
    """
    读取未分析的纠错信号，当同一条件积累 ≥3 次同方向纠错时，
    让 LLM 生成评估细则草稿，写入 job_criteria_notes（status=pending）。
    最后将所有涉及信号标记为已分析。
    """
    # 获取有足够信号的条件
    condition_counts = database.get_condition_correction_counts(job_id)
    if not condition_counts:
        return

    signal_ids_to_mark = []

    for row in condition_counts:
        cond_name = row["condition_name"]
        direction = row["direction"]
        count = row["cnt"]

        # 获取该条件的信号样本
        signals = database.get_correction_signals_for_condition(
            job_id, cond_name, direction, limit=10
        )
        if not signals:
            continue

        signal_ids_to_mark.extend(s["id"] for s in signals)

        # 构建 LLM 输入
        direction_label = "过于宽松（AI 通过但 HR 拒绝）" if direction == "too_loose" else "过于严格（AI 拒绝但 HR 通过）"
        examples = []
        for s in signals:
            parts = []
            if s.get("error_type"):
                type_map = {
                    "evidence_insufficient": "证据不足",
                    "criterion_misunderstood": "条件理解有误",
                    "criterion_not_important": "条件实际不重要",
                }
                parts.append(f"错误类型：{type_map.get(s['error_type'], s['error_type'])}")
            if s.get("evidence_text"):
                parts.append(f"AI 所用证据：{s['evidence_text'][:80]}")
            if s.get("hr_note"):
                parts.append(f"HR 备注：{s['hr_note'][:50]}")
            if parts:
                examples.append("- " + "；".join(parts))

        prompt = f"""你是一位招聘标准制定专家。以下是 HR 对 AI 在评估「{job_name}」岗位时，
针对条件「{cond_name}」的 {count} 次纠错记录，方向均为：{direction_label}。

纠错详情：
{chr(10).join(examples) or '（无详情）'}

请根据这些纠错，写一段简短的「评估细则」（50字以内），告诉 AI 未来应该如何评估这个条件，
包括：需要什么具体证据、什么程度算满足、什么不算。

同时判断这个条件是否应该设为硬性（候选人不满足即直接排除）。

输出 JSON：
{{"note_text": "细则内容（≤50字）", "is_hard": 0}}"""

        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是招聘评估标准制定专家，善于将模糊经验转化为可执行的判断细则。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
                timeout=60.0,
            )
            raw = response.choices[0].message.content
            parsed = json.loads(raw)
            note_text = (parsed.get("note_text") or "").strip()
            is_hard = int(bool(parsed.get("is_hard", 0)))

            if note_text and len(note_text) >= 5:
                database.upsert_criteria_note(
                    job_id=job_id,
                    condition_name=cond_name,
                    note_text=note_text,
                    is_hard=is_hard,
                    source_signal_count=count,
                )
                logger.info(f"生成评估细则草稿：{cond_name} ({direction}), is_hard={is_hard}")
        except Exception as e:
            logger.warning(f"细则生成 LLM 调用失败 [{cond_name}]: {e}")

    if signal_ids_to_mark:
        # 只标记已处理（≥3次）的信号；不足3次的留待继续累积
        database.mark_correction_signals_analyzed(signal_ids_to_mark)


def get_criteria_page_data(job_name):
    """
    供 /criteria/<job_name> 路由调用。
    返回该岗位的评估细则列表（按 status 分组）及校准统计。
    """
    job = database.get_job(job_name)
    if not job:
        return None
    job_id = job["id"]

    pending_notes = database.list_criteria_notes(job_id, status="pending")
    confirmed_notes = database.list_criteria_notes(job_id, status="confirmed")
    dismissed_notes = database.list_criteria_notes(job_id, status="dismissed")

    stats = database.get_criteria_override_stats(job_id)
    signal_counts = database.get_all_condition_signal_counts(job_id)
    stale_notes = database.get_stale_criteria_notes(job_id, days=30)
    effect_stats = database.get_criteria_effect_stats(job_id)

    return {
        "job_name": job_name,
        "pending": pending_notes,
        "confirmed": confirmed_notes,
        "dismissed": dismissed_notes,
        "dg_total": stats.get("dg_total", 0),
        "dg_reject_rate": stats.get("dg_reject_rate", 0.0),
        "yw_total": stats.get("yw_total", 0),
        "yw_approve_rate": stats.get("yw_approve_rate", 0.0),
        "signal_counts": signal_counts,
        "stale_notes": stale_notes,
        "effect_stats": effect_stats,
    }


# =============================================================================
# 偏好学习：信号分析（已废弃，由 analyze_correction_signals 取代）
# =============================================================================

def analyze_preference_signals(job_id, job_name, client):
    """
    读取未分析的偏好信号，让 LLM 提炼隐式规则，存入 preference_rules 表。
    """
    signals = database.get_unanalyzed_signals(job_id, limit=20)
    if not signals:
        return

    signal_ids = [s["id"] for s in signals]

    lines = []
    for s in signals:
        snap = _safe_loads(s.get("candidate_snapshot")) or {}
        stype = s.get("signal_type", "")
        ai_v = s.get("ai_verdict", "")
        hr_a = s.get("hr_action", "")
        dwell = s.get("dwell_seconds")
        tag = s.get("rejection_tag") or ""

        desc = ""
        if stype == "override_reject":
            desc = f"AI 评为「{ai_v}」→ HR 拒绝"
        elif stype == "override_approve":
            desc = f"AI 评为「{ai_v}」→ HR 通过"
        elif stype == "fast_approve":
            desc = f"快速通过（{dwell}秒）AI 评为「{ai_v}」"
        elif stype == "slow_reject":
            desc = f"犹豫后拒绝（{dwell}秒）原因：{tag}"

        candidate_info = (
            f"学历:{snap.get('first_degree','?')} "
            f"年限:{snap.get('total_years','?')}年 "
            f"行业:{','.join((snap.get('industry_tags') or [])[:2]) or '?'} "
            f"稳定性:{snap.get('avg_tenure_months','?')}月/段"
        )

        lines.append(f"- {desc} | {candidate_info}")

    prompt = f"""你是一位招聘偏好分析专家。以下是 HR 在筛选「{job_name}」岗位时的异常决策记录：

{chr(10).join(lines)}

请分析这些记录，推断 HR 可能存在的隐式偏好规则（不成文的判断标准）。
每条规则应当：
1. 简洁具体（20字以内）
2. 可验证（基于客观特征，非感觉）
3. 与官方 JD 不同（补充而非重复）

输出 JSON 格式：
{{"rules": [{{"text": "规则描述", "confidence": 0.0到1.0的置信度}}]}}

最多输出 3 条最有把握的规则，没有明显规律时输出空数组。"""

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "你是招聘决策分析专家，善于从行为数据中提炼隐式规则。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
            timeout=60.0,
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        rules = parsed.get("rules") or []
        for rule in rules:
            rt = (rule.get("text") or "").strip()
            conf = float(rule.get("confidence") or 0.5)
            if rt and len(rt) >= 5:
                database.upsert_preference_rule(job_id, rt, conf, len(signals))
    except Exception as e:
        logger.warning(f"偏好分析 LLM 调用失败: {e}")

    database.mark_signals_analyzed(signal_ids)
