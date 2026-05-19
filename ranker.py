"""
后处理排名引擎（动态权重调节器）
================================
基于 LLM 已经给出的 verdict（深绿/蓝色/黄色），在**同一 verdict 类别内部**
按用户调节的权重对候选人进行重新排序。零 API 调用、即时生效。

设计原则：
- **不跨 verdict 重排**：深绿组始终排在蓝色之前
- 仅影响展示顺序，不改变 verdict 本身
- 权重作用于已提取的 structured_json 字段
"""

import json
import logging

logger = logging.getLogger(__name__)


VERDICT_ORDER = {"深绿": 0, "蓝色": 1, "黄色": 2, "排除": 3}


# =============================================================================
# 维度评分（每个维度返回 0-100 的分数）
# =============================================================================

def _score_years(structured, target_years=5):
    """工龄维度：与目标年限的接近度，超过满分"""
    yrs = structured.get("total_work_years")
    if not isinstance(yrs, (int, float)) or yrs <= 0:
        return 50  # 缺失给中位
    if yrs >= target_years * 1.5:
        return 100
    if yrs >= target_years:
        return 90
    if yrs >= target_years * 0.6:
        return 70
    return 40


def _score_education(structured):
    """学历维度"""
    fd = (structured.get("first_degree") or "").strip()
    if "博士" in fd:
        return 100
    if "硕士" in fd:
        return 90
    if "全日制本科" in fd:
        return 80
    if "本科" in fd:
        return 65
    if "大专" in fd:
        return 45
    return 30


def _score_english(structured):
    """英语维度"""
    e = (structured.get("english_level") or "").strip()
    e_lc = e.lower()
    if "工作语言" in e or "专业八级" in e:
        return 100
    if "CET-6" in e or "六级" in e or "cet-6" in e_lc:
        return 85
    if "CET-4" in e or "四级" in e or "cet-4" in e_lc:
        return 65
    if "雅思" in e or "托福" in e:
        return 80
    if "未提及" in e or not e:
        return 40
    return 50


def _score_industry_match(structured, required_industries=None):
    """行业匹配度"""
    tags = structured.get("industry_tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    if not required_industries:
        return 70 if tags else 40
    hit = 0
    for r in required_industries:
        for t in tags:
            if r in t or t in r:
                hit += 1
                break
    if hit == len(required_industries):
        return 100
    if hit > 0:
        return 75
    return 30


def _score_stability(structured):
    """稳定性：平均每段经历时长"""
    work = structured.get("work_history") or []
    if not isinstance(work, list) or not work:
        return 50
    months = [w.get("months") for w in work
              if isinstance(w, dict) and isinstance(w.get("months"), int)]
    if not months:
        return 50
    avg = sum(months) / len(months)
    if avg >= 36:
        return 100
    if avg >= 24:
        return 80
    if avg >= 18:
        return 60
    if avg >= 12:
        return 40
    return 20


# =============================================================================
# 综合打分
# =============================================================================

def compute_score(structured, weights, required_industries=None,
                  target_years=5):
    """
    weights: dict，维度 → 权重(0-100)
    维度名：years / education / english / industry / stability
    返回该候选人的综合分数（加权平均，0-100）。
    """
    if not isinstance(structured, dict):
        return 0

    dims = {
        "years": _score_years(structured, target_years),
        "education": _score_education(structured),
        "english": _score_english(structured),
        "industry": _score_industry_match(structured, required_industries),
        "stability": _score_stability(structured),
    }

    total_w = 0.0
    weighted = 0.0
    for k, score in dims.items():
        w = float(weights.get(k, 50) or 0)
        if w <= 0:
            continue
        total_w += w
        weighted += w * score
    if total_w <= 0:
        return 0
    return round(weighted / total_w, 1)


def rank_candidates(candidates, weights, required_industries=None,
                    target_years=5):
    """
    输入：candidates list[dict]，每个 dict 至少有 verdict 与 structured_json（dict 或 str）
    输出：在每个 verdict 组内按 score 降序排列；组间按 verdict 自然顺序
    """
    grouped = {}
    for c in candidates:
        structured = c.get("structured_json")
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except Exception:
                structured = {}
        score = compute_score(structured or {}, weights,
                              required_industries=required_industries,
                              target_years=target_years)
        c["_rank_score"] = score
        verdict = c.get("verdict") or "黄色"
        grouped.setdefault(verdict, []).append(c)

    out = []
    for v in sorted(grouped.keys(), key=lambda x: VERDICT_ORDER.get(x, 99)):
        sub = sorted(grouped[v], key=lambda x: -x["_rank_score"])
        out.extend(sub)
    return out


DEFAULT_WEIGHTS = {
    "years": 70,
    "education": 60,
    "english": 50,
    "industry": 80,
    "stability": 40,
}


if __name__ == "__main__":
    print("Ranker 模块已加载")
    print(f"默认权重：{DEFAULT_WEIGHTS}")
