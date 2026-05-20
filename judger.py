"""
判断模块（v2 版）
==============
读取结构化简历 JSON，按岗位 prompt 进行评判。

输出新格式：
{
  "thinking": "AI 内部推理过程（不展示给 HR，仅用于审计）",
  "符合": [{"条件": "压铸行业经验", "证据": "原文：『...』"}, ...],
  "不符合": [{"条件": "英语要求", "原因": "简历无英语证书"}, ...],
  "has_hard_fail": false,
  "verdict": "深绿/蓝色/黄色/排除",
  "verdict_reason": "...，[描述笼统]?"  # 水分侦测标记会拼在结尾
}

判断逻辑：
  1. Few-Shot 增强（≥3 个录用样本时）
  2. Chain-of-Thought 双步推理：先逐条找原文证据，再汇总判定
  3. 水分侦测：若职责描述全为虚词（负责/参与/跟进）无量化数据，
     在 verdict_reason 末尾追加 [描述笼统]，不影响 verdict

兼容旧格式（pros/cons）：validate_judgment 同时填充新旧字段。
"""

import os
import sys
import json
import time
import logging

import database

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)


VALID_VERDICTS = {"深绿", "蓝色", "黄色", "排除"}


# =============================================================================
# Few-Shot 学习增强
# =============================================================================

def build_few_shot_section(job_name, max_examples=5):
    """
    从已录用/已通过候选人中取最多5个，构建 Few-Shot 参考。
    跳过 matches 为空的转移记录（质量低）。
    """
    all_examples = database.get_hired_candidates(job_name, limit=20)
    # 过滤掉转移进来的空 matches 记录
    examples = [e for e in all_examples
                if e.get("matches_json") and e["matches_json"] != "[]"][:max_examples]
    if not examples:
        return ""

    lines = [
        "",
        "# 历史录用案例（仅作背景参考）",
        "",
        "**重要约束**：以下是该岗位过去真正录用的候选人简要画像及符合原因。",
        "仅供理解录用偏好，不能凌驾于岗位要求和评估细则之上。",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        try:
            structured = json.loads(ex["structured_json"]) if ex.get("structured_json") else {}
        except Exception:
            structured = {}
        try:
            matches = json.loads(ex["matches_json"]) if ex.get("matches_json") else []
        except Exception:
            matches = []
        name = ex.get("name", "已录用候选人")
        wh = structured.get("work_history", []) or []
        years = structured.get("total_work_years", "?")
        first_degree = structured.get("first_degree", "?")
        english = structured.get("english_level", "?")
        industries = structured.get("industry_tags", []) or []
        roles = "、".join(
            f"{w.get('company','?')}({w.get('role','')})"
            for w in wh[:2] if isinstance(w, dict)
        )
        match_summary = "、".join(
            m.get("条件", "") for m in matches[:3] if m.get("条件")
        ) or "未记录"
        lines.append(
            f"- 案例{i}：{name} | {first_degree} | {years}年经验 | "
            f"英语:{english} | 行业:{','.join(industries[:3]) or '未知'} | "
            f"主要经历:{roles or '未明确'} | 录用主因:{match_summary}"
        )
    lines.append("")
    return "\n".join(lines)


def build_criteria_notes_section(job_name):
    """
    注入已确认的评估细则（job_criteria_notes），替代原偏好规则。
    细则分硬性（[硬性]）和软性（[软性]）两类，AI 必须按细则标准判断证据充分性。
    """
    job = database.get_job(job_name)
    if not job:
        return ""
    notes = database.get_confirmed_criteria_notes(job["id"])
    if not notes:
        return ""

    lines = [
        "",
        "# 该岗位评估细则（根据历史纠错积累，优先级高于岗位描述，严格执行）",
        "",
        "**说明**：以下细则规定了每个条件的证据充分性标准，你必须按此标准判断，",
        "不得自行调高或调低要求。标注 [硬性] 的条件若未通过，必须设 has_hard_fail=true。",
        "",
    ]
    for n in notes:
        hard_label = "[硬性]" if n["is_hard"] else "[软性]"
        lines.append(f"## {hard_label} 条件：{n['condition_name']}")
        lines.append(n["note_text"])
        lines.append("")
    return "\n".join(lines)


# =============================================================================
# 判断 prompt 构建
# =============================================================================

OUTPUT_INSTRUCTION = """
# 输出要求（严格执行）

## 第一步：内部推理（thinking 字段）
逐条扫描岗位要求，对每个要求：
1. 在简历的 work_history → responsibilities_verbatim、certifications、key_projects 等字段中
   寻找直接支持或否定该要求的**原文句子**。
2. 引用原文时使用「原文：『…』」格式，禁止改写或概括。
3. 找不到证据的要求，标注「无明确证据」。
4. 若存在评估细则，必须按细则中规定的证据标准判断，不得自行降低或提高要求。

## 第二步：汇总输出 JSON

```json
{
  "thinking": "（内部推理：第一步逐条扫描的过程，30-200字）",
  "符合": [
    {"条件": "压铸行业经验", "证据": "原文：『负责铝合金压铸件PPAP全流程』"}
  ],
  "不符合": [
    {"条件": "英语要求", "原因": "简历未提及任何英语证书或工作语言"}
  ],
  "has_hard_fail": false,
  "verdict": "深绿/蓝色/黄色/排除",
  "verdict_reason": "（一句话总结判定原因，不超过30字）"
}
```

## 输出措辞规则（强制）

- 每条「条件」与「证据/原因」**总长不超过 40 字**
- **禁止使用前缀废话**：禁止"此人/该候选人/建议考虑/可以说/总体而言/整体看"
- **禁止主观形容词**：禁止"优秀/良好/不错/一般/较强"
- 直接陈述事实

## verdict 取值规则

**硬性判定（has_hard_fail）**：
- 仅当评估细则中有 [硬性] 标记的条件，且候选人未通过该条件时，才设 has_hard_fail=true
- 没有评估细则时，不得自行将任何岗位要求升级为硬性条件
- has_hard_fail=true 时 verdict 必须为 "排除"

**等级判定**：
- "排除" = has_hard_fail 为 true
- "深绿" = 全部条件都有充分证据支持（包括评估细则中规定的证据标准）
- "蓝色" = 全部重要条件符合，一项次要条件轻微不足
- "黄色" = 多项条件证据不足或边界情况，需人工复核
"""


def build_judge_messages(structured_resume, job_prompt, job_name):
    """构建判断阶段的 LLM messages"""
    few_shot = build_few_shot_section(job_name)
    criteria_notes = build_criteria_notes_section(job_name)

    system_prompt = f"""{job_prompt}

{few_shot}

{criteria_notes}

# 输入数据说明

你将收到一份**已结构化的候选人简历 JSON**。
所有职责描述（responsibilities_verbatim）都是简历原文逐字保留，可作为证据来源。
请基于这些数据严格按规则评判。

{OUTPUT_INSTRUCTION}"""

    user_prompt = f"""请评判以下候选人是否适合「{job_name}」岗位：

```json
{json.dumps(structured_resume, ensure_ascii=False, indent=2)}
```

按规则逐条检查（先思考再下结论），输出 JSON。"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


# =============================================================================
# 输出校验与归一化
# =============================================================================

def _trim(s, max_len=40):
    s = (s or "").strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def _strip_filler(s):
    """去除常见废话前缀"""
    fillers = ["此人", "该候选人", "建议考虑", "可以说",
               "总体而言", "整体看", "综合来看", "总的来说"]
    s = (s or "").strip()
    for f in fillers:
        if s.startswith(f):
            s = s[len(f):].lstrip("，,。.：: ")
    return s


def _normalize_match_list(items, key_a, key_b):
    """归一化 list[{条件, 证据/原因}]，去废话、去超长"""
    out = []
    if not isinstance(items, list):
        return out
    for it in items:
        if isinstance(it, dict):
            cond = _trim(_strip_filler(it.get(key_a) or it.get("condition") or ""), 30)
            evid = _trim(_strip_filler(it.get(key_b) or it.get("evidence")
                                        or it.get("reason") or ""), 60)
            if cond or evid:
                out.append({key_a: cond, key_b: evid})
        elif isinstance(it, str):
            out.append({key_a: _trim(_strip_filler(it), 30), key_b: ""})
    return out


def _flatten_matches_to_pros(matches):
    """新格式 → 兼容旧 pros[] 字符串"""
    out = []
    for m in matches or []:
        cond = m.get("条件", "")
        evid = m.get("证据", "")
        if cond and evid:
            out.append(f"{cond}：{evid}")
        elif cond:
            out.append(cond)
        elif evid:
            out.append(evid)
    return out


def _flatten_mismatches_to_cons(mismatches):
    """新格式 → 兼容旧 cons[] 字符串"""
    out = []
    for m in mismatches or []:
        cond = m.get("条件", "")
        reason = m.get("原因", "")
        if cond and reason:
            out.append(f"{cond}：{reason}")
        elif cond:
            out.append(cond)
        elif reason:
            out.append(reason)
    return out


def validate_judgment(parsed):
    """确保 LLM 输出符合 schema，缺失字段补齐，新旧格式兼容"""
    if not isinstance(parsed, dict):
        return None

    # 优先读新格式 符合/不符合，兼容旧 pros/cons
    matches_raw = parsed.get("符合") or parsed.get("matches") or []
    mismatches_raw = parsed.get("不符合") or parsed.get("mismatches") or []
    matches = _normalize_match_list(matches_raw, "条件", "证据")
    mismatches = _normalize_match_list(mismatches_raw, "条件", "原因")

    # 旧格式兜底
    if not matches and parsed.get("pros"):
        matches = [{"条件": "", "证据": _trim(_strip_filler(str(p)), 80)}
                   for p in parsed["pros"] if str(p).strip()]
    if not mismatches and parsed.get("cons"):
        mismatches = [{"条件": "", "原因": _trim(_strip_filler(str(c)), 80)}
                      for c in parsed["cons"] if str(c).strip()]

    verdict = parsed.get("verdict", "黄色")
    if verdict not in VALID_VERDICTS:
        logger.warning(f"非法 verdict={verdict}，强制改为 黄色")
        verdict = "黄色"

    has_hard_fail = bool(parsed.get("has_hard_fail", False))

    # 一致性校验
    if has_hard_fail and verdict != "排除":
        logger.info(f"has_hard_fail=True 但 verdict={verdict}，强制改为 排除")
        verdict = "排除"
    if verdict == "排除" and not has_hard_fail:
        has_hard_fail = True

    verdict_reason = _trim(parsed.get("verdict_reason", ""), 100)

    pros = _flatten_matches_to_pros(matches)
    cons = _flatten_mismatches_to_cons(mismatches)

    return {
        "thinking": parsed.get("thinking", ""),
        "符合": matches,
        "不符合": mismatches,
        "pros": pros,
        "cons": cons,
        "has_hard_fail": has_hard_fail,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
    }


# =============================================================================
# 主入口
# =============================================================================

def judge(client, structured_resume, job_prompt, job_name, model="deepseek-chat"):
    messages = build_judge_messages(structured_resume, job_prompt, job_name)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        response_format={"type": "json_object"},
        timeout=120.0,
    )
    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"判断输出 JSON 解析失败: {e}")
        return None

    return validate_judgment(parsed)


def judge_with_retry(client, structured_resume, job_prompt, job_name,
                     max_retries=3, model="deepseek-chat"):
    for attempt in range(max_retries):
        try:
            result = judge(client, structured_resume, job_prompt, job_name,
                           model=model)
            if result:
                return result
        except Exception as e:
            logger.warning(f"判断失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(3 ** attempt)
    return {
        "thinking": "",
        "符合": [],
        "不符合": [{"条件": "AI异常", "原因": "评估调用失败，请人工复核"}],
        "pros": [],
        "cons": ["AI 评估异常，请人工复核"],
        "has_hard_fail": False,
        "verdict": "黄色",
        "verdict_reason": "AI 调用失败",
    }


if __name__ == "__main__":
    print("判断模块已加载（v2 新格式）")
    print(f"合法 verdict: {VALID_VERDICTS}")
