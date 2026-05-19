"""
JD 自动解析模块
=============
输入：岗位 JD 原文（工作职责 + 任职要求）
输出：结构化的 config_dict + prompt_text

用途：HR 在 Web 界面粘贴一段 JD，系统自动生成新岗位配置，无需手动写 prompt。
"""

import json
import logging

import init_jobs

logger = logging.getLogger(__name__)


JD_PARSE_PROMPT = """你是一位资深 HR 顾问，需要从 JD 原文中提炼结构化配置。

# 任务

阅读下方 JD（工作职责 + 任职要求），输出严格 JSON 配置。

# 关键提取项

1. **min_education**：最低学历。固定可选项：
   - "全日制本科"（默认值）
   - "全日制硕士"
   - "全日制大专"
2. **min_years**：最低工作年限（整数，单位"年"）
3. **english_required**：英语要求级别。仅可选：
   - "yes_strong" - 工作语言/CET-6/专业八级
   - "yes_basic" - CET-4 起步
   - "preferred" - 优先项（不强制）
   - "no" - 不要求
4. **english_threshold**：原文中关于英语的描述（中文）
5. **industry_required**：必须的行业（数组）。从 JD 中提取关键行业词，如 ["压铸"]、["汽车", "汽车零部件"]、["外贸"] 等。如果 JD 没明确行业要求，返回空数组 []
6. **requires_management**：是否要求团队管理经验（true/false）
7. **management_years**：要求管理经验年限（如有，否则0）
8. **key_responsibilities**：用一句话概括核心职责
9. **core_skills**：核心技能/工具/体系名词数组（如 ["IATF16949", "APQP", "PFMEA"]）
10. **extraction_focus**：候选人简历中应重点关注的信号点（中文一句话）
11. **rules**：评判规则数组，每条 {{name, description, type}}：
    - type="hard"：硬性，违反必淘汰（学历/年限/英语强要求/必须行业）
    - type="soft"：软性，影响评级
    - type="bonus"：加分项，不影响主判定

    **必须包含**：
    - 学历规则（hard）
    - 年限规则（hard，如 JD 有写）
    - 英语规则（hard 或 bonus，根据 english_required）
    - 至少 2 条软性规则（核心能力/工具/职责）

# 输出 JSON 格式

```json
{{
  "min_education": "全日制本科",
  "min_years": 3,
  "english_required": "yes_strong",
  "english_threshold": "英语可作为工作语言",
  "industry_required": ["压铸"],
  "industry_excluded": [],
  "requires_management": false,
  "management_years": 0,
  "key_responsibilities": "新项目可行性分析、APQP推进、客户技术对接",
  "core_skills": ["APQP", "DFM", "PFMEA"],
  "extraction_focus": "项目主导经历、APQP/DFM 经验、跨部门协调",
  "rules": [
    {{"name":"学历","description":"第一学历必须为全日制本科及以上","type":"hard"}},
    {{"name":"年限","description":"汽车行业压铸类相关产品经验≥3年","type":"hard"}},
    {{"name":"项目","description":"必须为项目Owner（主导项目全流程）","type":"soft"}}
  ]
}}
```

# 注意

- 学历规则统一为「第一学历必须为全日制本科及以上」（除非 JD 明确写"大专以上"才用大专）
- 不要编造 JD 中没有的要求
- rules 中的 description 应该具体、可验证（基于简历内容能判断的）
"""


def parse_jd(client, jd_text, model="deepseek-chat"):
    """
    把 JD 原文转为 config dict。
    返回：(config_dict: dict, error: str|None)
    """
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JD_PARSE_PROMPT},
            {"role": "user", "content": f"以下是 JD 原文：\n\n{jd_text}"},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
        timeout=90.0,
    )
    raw = response.choices[0].message.content
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"

    # 校验必要字段
    config.setdefault("min_education", "全日制本科")
    config.setdefault("min_years", 0)
    config.setdefault("english_required", "no")
    config.setdefault("industry_required", [])
    config.setdefault("rules", [])
    config.setdefault("target_count", 30)

    return config, None


def render_prompt_from_config(name, config):
    """根据 config 渲染最终 prompt（复用 init_jobs 的模板）"""
    rules = []
    for r in config.get("rules", []):
        rules.append((r.get("name", "?"),
                      r.get("description", ""),
                      r.get("type", "soft")))

    job_def = {
        "name": name,
        "key_responsibilities": config.get("key_responsibilities", ""),
        "core_skills": config.get("core_skills", []),
        "rules": rules,
    }
    return init_jobs.render_prompt(job_def)


def create_job_from_jd(client, name, jd_text):
    """
    解析 JD → 写入 job_configs/<name>/ + 数据库 jobs 表。
    返回 (config_dict | None, error_message | None)
    """
    import os
    config, err = parse_jd(client, jd_text)
    if err:
        return None, err

    config["name"] = name
    prompt_text = render_prompt_from_config(name, config)

    # 写入文件夹
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dir_name = init_jobs.safe_dirname(name)
    job_dir = os.path.join(base_dir, "job_configs", dir_name)
    os.makedirs(job_dir, exist_ok=True)
    with open(os.path.join(job_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    with open(os.path.join(job_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt_text)

    # 写入数据库
    import database
    database.upsert_job(name, config, prompt_text)

    return config, None
