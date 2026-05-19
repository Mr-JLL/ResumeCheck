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

def build_preference_section(job_name):
    """
    从 preference_rules 表中读取已确认（confirmed）的偏好规则，
    构建注入到 system prompt 的软性偏好段落。
    """
    job = database.get_job(job_name)
    job_id = job["id"] if job else None

    rules = database.list_preference_rules(job_id=job_id, status="confirmed")
    if not rules:
        return ""

    lines = [
        "",
        "# 过往筛选偏好（HR 行为总结，仅作软性参考）",
        "",
        "**说明**：以下偏好来自 HR 过去的录用/淘汰决策，置信度越高越值得重视。",
        "当候选人在以下维度明显符合或明显违背偏好时，可适当影响 verdict 边界判断。",
        "**但不能因此否定硬性规则的判定结论。**",
        "",
    ]
    for r in rules[:8]:  # 最多注入8条，防止 prompt 过长
        conf_pct = int(r["confidence"] * 100)
        lines.append(f"- {r['rule_text']}（置信度 {conf_pct}%，样本数 {r['evidence_count']}）")
    lines.append("")
    return "\n".join(lines)


def build_few_shot_section(job_name, max_examples=3):
    """
    数据库中若有该岗位的录用样本（hired/approved），构建 Few-Shot 参考。
    只展示关键事实，不展示历史 pros，避免污染当前规则。
    """
    examples = database.get_hired_candidates(job_name, limit=max_examples)
    if not examples:
        return ""

    lines = [
        "",
        "# 历史录用案例（仅作背景参考）",
        "",
        "**重要约束**：以下是该岗位过去真正录用的候选人简要画像。",
        "它们仅供你**理解公司过往的录用偏好**，**不能凌驾于上面的评判规则之上**。",
        "如果当前候选人不符合上面的规则，即使与下方案例相似，也必须按规则判定。",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        try:
            structured = json.loads(ex["structured_json"]) if ex.get("structured_json") else {}
        except Exception:
            structured = {}
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
        lines.append(
            f"- 案例{i}：{name} | {first_degree} | {years}年经验 | "
            f"英语:{english} | 行业:{','.join(industries[:3]) or '未知'} | "
            f"主要经历:{roles or '未明确'}"
        )
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# 判断 prompt 构建
# =============================================================================

OUTPUT_INSTRUCTION = """
# 输出要求（严格执行）

## 第一步：内部推理（thinking 字段）
逐条扫描岗位的硬性/软性要求，对每个要求：
1. 在简历的 work_history → responsibilities_verbatim、certifications、key_projects 等字段中
   寻找直接支持或否定该要求的**原文句子**。
2. 引用原文时使用「原文：『…』」格式，禁止改写或概括。
3. 找不到证据的要求，标注「无明确证据」。

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

## 水分侦测（在 verdict_reason 中标注）

若该候选人 work_history 各段的 responsibilities_verbatim 主要是「负责/参与/协助/跟进/支持」
等虚词，**几乎没有任何量化数据**（百分比、时间、数量、金额），
在 verdict_reason 末尾追加 ` [描述笼统]` 标记。
**这个标记不影响 verdict**，仅作信息提示。

## verdict 取值

- "排除" = 任何硬性要求不满足（has_hard_fail 必须为 true）
- "深绿" = 全部硬性 + 全部软性要求都符合
- "蓝色" = 全部硬性符合 + 一项软性轻微不足
- "黄色" = 多项软性不足或边界情况，需人工复核
"""


def build_judge_messages(structured_resume, job_prompt, job_name):
    """构建判断阶段的 LLM messages"""
    few_shot = build_few_shot_section(job_name)
    preference = build_preference_section(job_name)

    system_prompt = f"""{job_prompt}

{few_shot}

{preference}

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
