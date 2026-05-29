"""
判断模块（v3 版）
==============
读取结构化简历 JSON，按岗位 prompt + 动态规则段进行评判。

输出格式：
{
  "thinking": "AI 内部推理过程（不展示给 HR，仅用于审计）",
  "符合": [{"条件": "压铸行业经验", "证据": "原文：『...』"}, ...],
  "不符合": [{"条件": "英语要求", "原因": "简历无英语证书"}, ...],
  "has_hard_fail": false,
  "verdict": "深绿/蓝色/黄色/排除",
  "verdict_reason": "...，[描述笼统]?"  # 水分侦测标记会拼在结尾
}

判断逻辑：
  1. Chain-of-Thought 双步推理：先逐条找原文证据，再汇总判定
  2. 水分侦测：若职责描述全为虚词（负责/参与/跟进）无量化数据，
     在 verdict_reason 末尾追加 [描述笼统]，不影响 verdict
  3. 评判规则从 config_json.rules 动态渲染，job_prompt 只存上下文

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

_STRIP_SECTIONS = {"# 评判规则", "# 评判逻辑", "# 输出格式", "# 重要约束"}


def _clean_job_prompt(prompt_text):
    """从旧 prompt 中剥离遗留的 # 评判规则 / # 评判逻辑 / # 输出格式 / # 重要约束 段落。"""
    if not prompt_text:
        return ""
    lines = prompt_text.split("\n")
    result = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(h) for h in _STRIP_SECTIONS):
            skipping = True
            continue
        if skipping and stripped.startswith("# ") and not stripped.startswith("## "):
            skipping = False
        if not skipping:
            result.append(line)
    return "\n".join(result).strip()


def build_rules_section(job_name):
    """从 config_json.rules 动态渲染评判规则段（含 evidence_hint）。"""
    job = database.get_job(job_name)
    if not job:
        return ""
    try:
        config = json.loads(job.get("config_json") or "{}")
    except Exception:
        return ""
    rules = config.get("rules", [])
    if not rules:
        return ""
    type_tag = {"hard": "🔴 硬性", "soft": "🟡 软性", "bonus": "🟢 加分"}
    lines = ["", "# 评判规则（按硬性 → 软性 → 加分顺序）", ""]
    for i, r in enumerate(rules, 1):
        name = r.get("name", "")
        desc = r.get("description", "")
        kind = r.get("type", "soft")
        hint = (r.get("evidence_hint") or "").strip()
        tag = type_tag.get(kind, "🟡 软性")
        lines.append(f"## 规则{i}（{tag}）：{name}")
        lines.append(desc)
        if hint:
            lines.append(f"*证据标准*：{hint}")
        lines.append("")
    lines.append("**颜色判定逻辑（严格按此执行，详见输出要求第三步）**：")
    lines.append("1. 任一 🔴硬性条件不满足 → has_hard_fail=true → verdict='排除'")
    lines.append("   ↳ 若年限差距≤0.5年且所有软性均满足，verdict仍='排除'，")
    lines.append("     在verdict_reason末尾追加：[边缘案例-差X月-建议HR复议]")
    lines.append("2. 全部 🟡软性满足/部分满足 → 深绿；1条不满足 → 蓝色；2条及以上不满足 → 黄色")
    lines.append("3. 🟢加分满足 → 不改变颜色，在verdict_reason追加★，仅作同色组排序参考")
    lines.append("")
    return "\n".join(lines)


OUTPUT_INSTRUCTION = """
# 输出要求（严格按以下步骤执行，不可跳步）

## 第零步：简历密度评估
首先评估简历整体表达密度，在 thinking 字段首行标注：
- `[简历密度：低]` → 全篇用短句罗列职责，无展开性描述
- `[简历密度：中]` → 部分岗位有展开描述，部分仅列职责
- `[简历密度：高]` → 大多数岗位有具体项目/数字/流程描述

**密度低时**：判断标准是"是否有真实经历迹象"而非"描述是否详细"，允许以较少细节认定条件满足。

## 第一步：逐条提取原文证据（只提取，不判断）
对每个评估条件，从 work_history → responsibilities_verbatim、certifications、key_projects 等字段中：
- 找到相关原文：使用格式 `原文：『…』`
- 找不到：写 `未见相关描述`

## 第二步：逐条证据质量判断（仅基于第一步摘录，不重新读简历）
对每条规则判断：满足 / 部分满足 / 不满足，并标注证据强度（强/弱）：
- **强**：有具体项目/独立操作/可量化描述
- **弱**：有相关经历迹象但不够具体（简历密度低时允许弱证据认定满足）

## 第三步：颜色由以下确定性规则自动得出（不得自行偏离）

**年限计算规则**（适用于所有年限类硬性条件）：
- 只写起止年月（如 2020.03-2022.07）：精确按月计算
- 只写起止年份（如 2020-2022）：默认取1月1日计算
- "至今"：等于2026年5月
- 重叠期：取最长连续时间段，不累加
- 明确标注为实习期：不计入正式工作年限

**颜色判定**：
- 任一 🔴硬性条件（规则标注为🔴硬性，或评估细则标注为[硬性]）不满足
  → has_hard_fail=true，verdict="排除"
  → 特殊：若年限差距≤0.5年，且所有软性条件均满足，verdict仍="排除"，
    在verdict_reason末尾追加：[边缘案例-差X月-建议HR复议]
- 全部 🟡软性条件满足或部分满足 → verdict="深绿"
- 1条 🟡软性条件不满足 → verdict="蓝色"
- 2条及以上 🟡软性条件不满足 → verdict="黄色"
- 🟢加分条件满足 → 不改变颜色，在verdict_reason中追加★，仅作同色组排序参考

## 疑点检测（不影响颜色评级，如有须如实标注）
检查以下情况，有则写入 疑点 字段（无则输出空列表 []）：
- **跳槽频繁**：任意连续5年内有3份以上工作经历，且每份均不足18个月
- **描述密度矛盾**：声称掌握≥4项专业工具，但全篇无任何具体工作场景描述
- **时间线空白**：工作经历中有超过6个月的空白期

## 汇总输出 JSON

```json
{
  "thinking": "（第零步密度标注 + 第一步各条原文摘录 + 第二步各条质量判断，50-300字）",
  "符合": [
    {"条件": "行业", "证据": "原文：『在某汽车压铸件厂担任质量工程师』"}
  ],
  "不符合": [
    {"条件": "工具", "原因": "未见SPC/MSA具体应用描述"}
  ],
  "疑点": ["跳槽频繁：2019-2024共4份工作，最长18个月"],
  "has_hard_fail": false,
  "verdict": "深绿/蓝色/黄色/排除",
  "verdict_reason": "（一句话总结，不超过30字；★追加在此；边缘案例注记追加在此）"
}
```

## 输出措辞规则（强制）
- 每条「条件」与「证据/原因」**总长不超过 40 字**
- **禁止使用前缀废话**：禁止"此人/该候选人/建议考虑/可以说/总体而言/整体看"
- **禁止主观形容词**：禁止"优秀/良好/不错/一般/较强"
- 直接陈述事实
"""


def build_judge_messages(structured_resume, job_prompt, job_name):
    """构建判断阶段的 LLM messages"""
    clean_prompt = _clean_job_prompt(job_prompt)
    rules_section = build_rules_section(job_name)
    criteria_notes = build_criteria_notes_section(job_name)

    system_prompt = f"""{clean_prompt}

{rules_section}

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

    # 处理疑点：合并入 mismatches，加 "[疑点]" 前缀，不影响颜色
    flags = parsed.get("疑点") or []
    if isinstance(flags, list):
        for flag in flags:
            flag_str = str(flag).strip()
            if flag_str:
                mismatches.append({"条件": "[疑点]", "原因": _trim(_strip_filler(flag_str), 60)})

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
