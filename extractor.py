"""
结构化简历提取模块
================
替代旧的「文本蒸馏」步骤。
将原始简历 HTML/文本转为完整结构化 JSON，所有职责原句逐字保留，
不做任何主观压缩或重述。

输出 schema:
{
  "name": "...",
  "age": int或null,
  "first_degree": "全日制本科" / "全日制大专" / "全日制硕士" / "成人本科" / "未明确",
  "highest_degree": "...",
  "school": "...",          # 第一学历的学校
  "major": "...",
  "is_overseas_education": bool,
  "english_level": "CET-4/CET-6/专业八级/雅思/托福/工作语言/未提及",
  "english_evidence": "原文片段",
  "total_work_years": int,
  "work_history": [
    {
      "company": "...",
      "role": "...",
      "period": "2018-2023",
      "months": int,
      "is_current": bool,
      "responsibilities_verbatim": ["原文句子1", "原文句子2", ...]
    }
  ],
  "certifications": ["IATF16949内审员", ...],
  "key_projects": ["项目原文描述", ...],
  "industry_tags": ["压铸", "汽车零部件", ...]
}
"""

import os
import sys
import re
import json
import logging
import datetime
from bs4 import BeautifulSoup

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)


# =============================================================================
# HTML 清洗
# =============================================================================

def clean_html_to_text(html_raw):
    """从 51job 简历 HTML 中提取干净文本（去水印、去脚本、去样式）"""
    soup = BeautifulSoup(html_raw, "html.parser")
    for trash in soup(["script", "style", "img", "svg", "noscript", "meta",
                       "link", "iframe"]):
        trash.decompose()
    text = soup.get_text(separator="\n", strip=True)

    watermark_patterns = [
        r"仅限.*?招聘使用[\s\S]{0,80}?[A-F0-9]{4,}",
        r"[A-F0-9]{30,}",
        r"招聘专用",
        r"操作时间:\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}\s+操作人:[a-f0-9]+",
        r"声明:该人选信息仅供公司招聘使用.*?封禁",
    ]
    for pat in watermark_patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# =============================================================================
# 结构化提取 prompt
# =============================================================================

EXTRACTION_PROMPT = """你是一个无情且精准的简历结构化提取器。

任务：将下面的简历原文转为结构化 JSON，**所有职责描述必须逐字逐句保留原文**，禁止概括、改写、合并。

# 严格要求

1. **work_history 中每段经历的 responsibilities_verbatim 必须是原文句子的完整列表**，不要截断、不要总结。
   即使原文有10句话，也要全部保留。
2. **first_degree（第一学历）= 候选人最早的全日制学历**（从工作前的教育经历推断）。
   - 如果有多个学位，"第一学历"指**第一个全日制学位**（通常是大学本科或大专的初次教育）
   - 区分「全日制」与「成人/函授/在职」
   - 取值范围："全日制本科" / "全日制硕士" / "全日制博士" / "全日制大专" / "成人本科" / "成人大专" / "高中" / "未明确"
3. **highest_degree = 最高学历**（可能晚于第一学历）
4. **age 字段**：找简历中明确写的年龄数字。如果只有出生年月，按当前年份({current_year})计算
5. **english_level**：综合判断
   - "工作语言" = 简历明确写"英语作为工作语言"或在外资工作多年
   - "专业八级" / "CET-6" / "CET-4" = 有明确证书
   - "雅思XX" / "托福XX" = 有具体分数
   - "未提及" = 简历完全没提英语
6. **work_history 时间段**：用 YYYY.MM-YYYY.MM 或 YYYY.MM-至今 格式
7. **industry_tags**：从工作经历中识别出的行业标签（如"压铸"、"汽车零部件"、"电子"、"模具"、"机加工"等）
8. **key_projects**：如果简历有「项目经验」单独章节，抽取每个项目的原文描述

# 输出 JSON schema

```json
{{
  "name": "姓名",
  "age": 数字或null,
  "first_degree": "全日制本科",
  "highest_degree": "全日制硕士",
  "school": "第一学历学校",
  "major": "第一学历专业",
  "is_overseas_education": false,
  "english_level": "CET-6",
  "english_evidence": "原文中关于英语的片段",
  "total_work_years": 9,
  "work_history": [
    {{
      "company": "广东鸿图科技股份有限公司",
      "role": "项目工程师",
      "period": "2018.06-2023.08",
      "months": 62,
      "is_current": false,
      "responsibilities_verbatim": [
        "独立负责铝合金压铸件新品从报价到PPAP全流程",
        "协调模具厂、供应商完成3套新模试制验收",
        "..."
      ]
    }}
  ],
  "certifications": ["IATF16949内审员", "PMP"],
  "key_projects": [
    "2021年主导XXX项目，XXX..."
  ],
  "industry_tags": ["压铸", "汽车零部件"]
}}
```

# 容错规则

- 找不到的字段写 null（数字）或 "未明确"（字符串）或 [] （列表）
- 不要编造任何信息
- 如果某段经历的职责描述只有一两句，也老老实实保留这一两句
"""


def build_extraction_messages(resume_text):
    current_year = datetime.datetime.now().year
    return [
        {"role": "system",
         "content": EXTRACTION_PROMPT.format(current_year=current_year)},
        {"role": "user", "content": f"请提取以下简历内容：\n\n{resume_text}"},
    ]


# =============================================================================
# Python 端校验与补全
# =============================================================================

def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _calc_months(period_str):
    """从 'YYYY.MM-YYYY.MM' 或 'YYYY.MM-至今' 计算月数"""
    if not period_str or not isinstance(period_str, str):
        return 0
    m = re.match(r"(\d{4})[.\-/年](\d{1,2})[月]?\s*[-—~至到]\s*(\d{4}|至今|现在)[.\-/年]?(\d{1,2})?",
                 period_str)
    if not m:
        return 0
    y1 = _safe_int(m.group(1))
    m1 = _safe_int(m.group(2))
    end_part = m.group(3)
    if end_part in ("至今", "现在"):
        now = datetime.datetime.now()
        y2, m2 = now.year, now.month
    else:
        y2 = _safe_int(end_part)
        m2 = _safe_int(m.group(4)) if m.group(4) else 1
    if not (y1 and m1 and y2 and m2):
        return 0
    return max(0, (y2 - y1) * 12 + (m2 - m1))


# =============================================================================
# 学历归一化（纯字符串，无 LLM）
# =============================================================================

_ADULT_SIGNALS = ("成人", "函授", "在职", "电大", "夜大", "自考", "业余")

# 按层级从高到低匹配，避免"博士研究生"被"研究生"先命中再被"博士"覆盖
_DEGREE_TIERS = [
    # (关键词元组,          全日制 canonical,  成人/非全日制 canonical)
    (("博士", "phd", "doctor"),                   "全日制博士", "成人博士"),
    (("硕士", "研究生", "mba", "mpa", "master"),  "全日制硕士", "成人硕士"),
    (("本科", "学士", "bachelor"),                "全日制本科", "成人本科"),
    (("大专", "专科", "高职"),                    "全日制大专", "成人大专"),
    (("高中", "中专", "技校", "职高", "中职", "初中"), "高中",   "高中"),
]


def _normalize_degree(val):
    """
    将 LLM 输出的任意第一学历描述归一化为规则引擎可识别的 canonical 值。
    策略：有明确"成人/函授"信号才标注非全日制，否则保守默认全日制。
    无法识别的值原样返回，规则引擎遇到未知值时会保守放行。
    """
    if not val or val in ("未明确", ""):
        return val
    is_adult = any(sig in val for sig in _ADULT_SIGNALS)
    val_lower = val.lower()
    for keywords, fulltime, adult in _DEGREE_TIERS:
        if any(k in val_lower for k in keywords):
            return adult if is_adult else fulltime
    return val


def post_validate(parsed):
    """对 LLM 输出做基础校验和补全"""
    if not isinstance(parsed, dict):
        return parsed

    # 工作年限校核
    work_history = parsed.get("work_history", [])
    if isinstance(work_history, list):
        total_months = 0
        for w in work_history:
            if not isinstance(w, dict):
                continue
            m = w.get("months")
            if not isinstance(m, int) or m <= 0:
                m = _calc_months(w.get("period", ""))
                w["months"] = m
            total_months += m
        # 如果 LLM 没给 total_work_years，从 work_history 推算
        if not parsed.get("total_work_years"):
            parsed["total_work_years"] = round(total_months / 12)

    # first_degree 兜底 + 归一化
    if not parsed.get("first_degree"):
        parsed["first_degree"] = "未明确"
    else:
        parsed["first_degree"] = _normalize_degree(parsed["first_degree"])

    # 默认值
    parsed.setdefault("certifications", [])
    parsed.setdefault("key_projects", [])
    parsed.setdefault("industry_tags", [])
    parsed.setdefault("english_level", "未提及")

    return parsed


# =============================================================================
# 主入口
# =============================================================================

def extract_structured(client, html_raw, model="deepseek-chat"):
    """
    输入：51job 原始 HTML + LLM client
    输出：结构化简历 JSON（dict）
    """
    resume_text = clean_html_to_text(html_raw)

    # 简历过长时截断（DeepSeek 64K 上限够用，但留余量）
    max_chars = 20000
    if len(resume_text) > max_chars:
        logger.warning(f"简历过长（{len(resume_text)}字），截断到 {max_chars}")
        resume_text = resume_text[:max_chars]

    messages = build_extraction_messages(resume_text)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        response_format={"type": "json_object"},
        timeout=90.0,
    )
    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"结构化提取 JSON 解析失败: {e}")
        return None

    return post_validate(parsed)


def extract_with_retry(client, html_raw, max_retries=3, model="deepseek-chat"):
    import time
    for attempt in range(max_retries):
        try:
            result = extract_structured(client, html_raw, model=model)
            if result:
                return result
        except Exception as e:
            logger.warning(f"结构化提取失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(3 ** attempt)
    return None


def generate_fingerprint(structured: dict, client, model: str = "deepseek-chat") -> str:
    """
    基于结构化简历生成岗位无关的能力指纹（150字以内）。
    工作经历为空或职责描述不足2句时返回空字符串（信息不足，不生成）。
    """
    work_history = structured.get("work_history") or []
    total_sentences = sum(
        len(w.get("responsibilities_verbatim") or []) for w in work_history
    )
    if not work_history or total_sentences < 2:
        return ""

    key_fields = {
        "name": structured.get("name"),
        "age": structured.get("age"),
        "first_degree": structured.get("first_degree"),
        "school": structured.get("school"),
        "total_work_years": structured.get("total_work_years"),
        "english_level": structured.get("english_level"),
        "industry_tags": structured.get("industry_tags", []),
        "certifications": structured.get("certifications", []),
        "key_projects": (structured.get("key_projects") or [])[:2],
        "work_history": [
            {
                "company": w.get("company"),
                "role": w.get("role"),
                "months": w.get("months"),
                "responsibilities_verbatim": (w.get("responsibilities_verbatim") or [])[:3],
            }
            for w in work_history[:4]
        ],
    }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是简历分析助手。请用150字以内的中文，为以下候选人写一段「能力指纹」。\n"
                        "要求：\n"
                        "1. 只描述这个人「是什么样的人」，不针对任何具体岗位评价\n"
                        "2. 必须包含：核心行业/技术背景、主要技能或成就（有数字最好）、学历\n"
                        "3. 如有明显弱点（频繁跳槽、学历偏低等）也要写出\n"
                        "4. 语言客观具体，禁止使用「较强」「丰富」「出色」等模糊形容词\n"
                        "5. 输出纯文字，不要标题或分点，不超过150字"
                    ),
                },
                {
                    "role": "user",
                    "content": f"候选人信息（JSON）：\n{json.dumps(key_fields, ensure_ascii=False)}",
                },
            ],
            max_tokens=250,
            temperature=0.1,
        )
        fp = resp.choices[0].message.content.strip()
        return fp[:300] if fp else ""
    except Exception as e:
        logger.warning(f"指纹生成 LLM 调用失败: {e}")
        return ""


if __name__ == "__main__":
    # 简易测试
    print("结构化提取模块已加载")
    print(f"提取 prompt 长度: {len(EXTRACTION_PROMPT)} 字符")
