"""
13个岗位的初始化配置生成器
========================
依据华阳精机《职位描述+任职要求》一次性生成所有岗位的：
  - job_configs/<岗位>/config.json    (硬性指标 + 评判参数)
  - job_configs/<岗位>/prompt.txt     (LLM 评判 prompt)
并将岗位写入 SQLite 数据库 jobs 表。

后续可通过 Web 界面「新增岗位」（粘贴 JD）追加岗位。
本脚本可重复运行，已存在的岗位将被更新。
"""

import os
import sys
import json
import database

# Windows 控制台编码兼容
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# =============================================================================
# 13 个岗位的核心定义
# =============================================================================

JOBS = [
    # ─────────────────────────────────────────────
    {
        "name": "市场开拓（欧美系）",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "yes_strong",
        "english_threshold": "英语专业八级，具备商务谈判能力",
        "industry_required": ["汽车", "汽车零部件", "汽车配件"],
        "industry_excluded": ["互联网", "金融", "餐饮", "教育", "房地产"],
        "key_responsibilities": "欧美系汽车客户市场开拓、客户关系维护、市场活动策划",
        "core_skills": ["欧美市场开发", "汽车B2B", "汽车零部件流程", "商务谈判"],
        "preferred_skills": ["国外留学", "国外工作经历"],
        "extraction_focus": "客户开拓、市场策划、商务谈判、英语水平、汽车行业经历、留学/海外经历",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，且为英语相关专业", "hard"),
            ("英语", "英语专业八级，具备商务谈判能力，能口语+书面流利沟通", "hard"),
            ("年限", "汽车行业欧美市场开发经验≥3年", "hard"),
            ("行业", "在汽车/汽车零部件行业从业，熟悉制造业B2B交易模式", "hard"),
            ("加分", "有国外留学或工作经历", "bonus"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "市场内务",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 1,
        "english_required": "yes_strong",
        "english_threshold": "英语听说读写流利，能作为工作语言",
        "industry_required": ["汽车", "外贸"],
        "key_responsibilities": "国内外客户订单交付管理、对账收款、样品/量产协调、客户来访接待",
        "core_skills": ["外贸跟单", "采购", "订单管理", "客户接待", "英语沟通"],
        "extraction_focus": "外贸跟单、采购、订单管理、客户接待、英语水平、汽车外贸经历",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，英语或商务英语相关专业", "hard"),
            ("英语", "英语听说读写流利，能作为工作语言", "hard"),
            ("年限", "外贸跟单/采购经验≥1年（最好≥2年），或汽车外贸经验", "hard"),
            ("能力", "熟悉市场跟单工作流程", "soft"),
            ("加分", "汽车外贸经验", "bonus"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "质量经理-主管",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 8,
        "management_years": 4,
        "requires_management": True,
        "english_required": "yes_strong",
        "english_threshold": "英语听说读写可作为工作语言无障碍沟通",
        "industry_required": ["汽车", "汽车零部件", "压铸"],
        "key_responsibilities": "质量全过程管控、IATF16949体系维护、客诉处理、团队管理",
        "core_skills": ["IATF16949", "客诉处理", "8D报告", "质量分析", "团队管理", "供应商质量管理"],
        "extraction_focus": "团队管理规模、IATF体系、客诉处理、8D报告、批量不良/客户退货、根本原因分析",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械类专业", "hard"),
            ("年限", "汽车行业压铸类相关产品经验≥8年", "hard"),
            ("管理", "团队管理经验≥4年", "hard"),
            ("英语", "英语听说读写可作为工作语言", "hard"),
            ("体系", "主导或参与过IATF16949等质量体系认证及维护", "soft"),
            ("能力", "处理过重大质量问题（批量不良/客户退货），具备根本原因分析及预防措施经验", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "质量工程师",
        "target_count": 50,
        "min_education": "全日制本科",
        "min_years": 2,
        "english_required": "preferred",
        "english_threshold": "英语CET-6以上，能作为工作语言（优先项）",
        "industry_required": ["汽车", "汽车零部件", "压铸"],
        "key_responsibilities": "新产品质量控制、PPAP/MSA/CPK/PPK、过程质量控制、客诉8D、现场质量培训",
        "core_skills": ["IATF16949", "VDA6.3", "APQP", "PPAP", "PFMEA", "SPC", "MSA", "8D"],
        "extraction_focus": "PPAP/MSA/CPK/PPK、五大工具、IATF16949、VDA6.3、客诉处理、8D报告、质量改善",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械类专业", "hard"),
            ("年限", "汽车行业压铸类相关产品经验≥2年", "hard"),
            ("行业", "汽车/汽车零部件/压铸行业", "hard"),
            ("体系", "熟悉IATF16949以及VDA6.3要求", "soft"),
            ("工具", "掌握质量五大工具：APQP/PPAP/PFMEA/SPC/MSA", "soft"),
            ("英语", "英语CET-6以上，能作为工作语言（优先项）", "bonus"),
            ("能力", "能独立处理客户反馈并协调内部落实改善", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "体系工程师",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "no",
        "industry_required": [],
        "key_responsibilities": "公司体系第三方认证、审核、质量目标统计、文件管理",
        "core_skills": ["IATF16949", "内审", "外审", "体系文件编制", "质量手册"],
        "extraction_focus": "体系管理年限、IATF16949内审员证书、内审/外审组织、纠正措施、体系文件编制",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，理工科相关（机械、质量管理等）", "hard"),
            ("年限", "体系管理或相关领域经验≥3年", "hard"),
            ("证书", "具有IATF16949等体系内审员证书（优先）", "bonus"),
            ("能力", "独立组织内审、外审，跟踪纠正措施", "soft"),
            ("文件", "熟练编制体系文件（质量手册、程序文件、作业指导书）", "soft"),
            ("职称", "具有初级/中级工程师职称（优先）", "bonus"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "项目工程师",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "yes_strong",
        "english_threshold": "英语可作为工作语言沟通；有较强英文读写能力",
        "industry_required": ["汽车", "汽车零部件", "压铸"],
        "key_responsibilities": "新项目可行性分析、APQP推进、DFM、PPAP文件包、客户技术对接、VDA6.3审核",
        "core_skills": ["APQP", "DFM", "PFMEA", "PPAP", "DR/TR评审", "VDA6.3", "项目主导"],
        "extraction_focus": "项目主导经历（Owner vs 参与）、APQP/DFM、跨部门协调、客户审核、技术评审、PPAP文件包",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械类专业", "hard"),
            ("年限", "汽车行业压铸类相关产品经验≥3年", "hard"),
            ("英语", "英语可作为工作语言沟通；能编写中英文项目资料", "hard"),
            ("项目", "必须为项目Owner（主导项目全流程），而非项目参与者", "soft"),
            ("流程", "熟悉IATF16949及五大工具，熟悉汽车产品/模具开发流程", "soft"),
            ("能力", "能进行新项目图纸评审、策划与开发", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "生产项目主管",
        "target_count": 20,
        "min_education": "全日制本科",
        "min_years": 5,
        "requires_management": True,
        "english_required": "no",
        "industry_required": ["压铸"],
        "key_responsibilities": "新产品量产达成、量产质量改善、模具/设备/工装管理、技术研发管理",
        "core_skills": ["量产管理", "新产品达成", "压铸工艺", "模具管理", "产能管理"],
        "extraction_focus": "量产计划与对接、压铸车间技术管理、模具寿命管理、产能数据、技术培训",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械工程等相关专业", "hard"),
            ("年限", "压铸行业相关工作经验≥5年", "hard"),
            ("管理", "有管理经验（优先）", "bonus"),
            ("工艺", "熟练掌握压铸工艺及其设备，具备生产技术管理经验", "soft"),
            ("能力", "良好沟通与跨部门协调能力", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "压铸主管",
        "target_count": 20,
        "min_education": "全日制本科",
        "min_years": 5,
        "requires_management": True,
        "management_years": 5,
        "english_required": "no",
        "industry_required": ["压铸"],
        "key_responsibilities": "压铸车间日常运营管理、生产计划、员工管理培训、生产过程控制、成本控制",
        "core_skills": ["压铸车间管理", "生产计划", "员工培训", "成本控制", "压铸工艺"],
        "extraction_focus": "压铸车间管理规模、生产计划与排程、员工培训、成本控制、压铸工艺现场管理",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上", "hard"),
            ("年限", "压铸管理经验≥5年", "hard"),
            ("管理", "有团队管理经验和领导能力", "hard"),
            ("工艺", "熟悉压铸生产工艺流程，对压铸产品生产过程有深入了解", "soft"),
            ("能力", "具备生产计划和排程能力", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "压铸工程师",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "no",
        "industry_required": ["汽车", "压铸"],
        "key_responsibilities": "压铸件工艺优化、过程改进、压铸机调试维护、工艺文件、项目设计评审",
        "core_skills": ["压铸工艺", "压铸机调试", "工艺文件编制", "过程改进"],
        "extraction_focus": "压铸工艺优化、压铸机型号与调试经历、工艺文件编制、电气知识、AutoCAD",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械类专业", "hard"),
            ("年限", "汽车行业压铸类相关产品经验≥3年", "hard"),
            ("行业", "汽车压铸行业", "hard"),
            ("工艺", "熟悉压铸工艺流程，具备技术知识和实践经验", "soft"),
            ("电气", "具备基本电气知识，了解压铸机工作原理", "soft"),
            ("软件", "熟练使用 Word/Excel/AutoCAD", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "CNC工程师",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "no",
        "industry_required": [],
        "key_responsibilities": "CNC加工工艺开发、加工方案制定、机床程序调试、操作人员培训、新项目导入",
        "core_skills": ["CNC加工", "UG", "Pro/E", "MasterCam", "AutoCAD", "数控编程"],
        "extraction_focus": "CNC加工经验、UG/Pro-en/MasterCam/CAD熟练度、卧加调试、五轴联动",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械设计制造、数控、模具设计制造等相关专业", "hard"),
            ("年限", "CNC工作经验≥3年", "hard"),
            ("软件", "熟练使用 UG/Pro-en/MasterCam/CAD 进行绘图建模、加工程序编制", "soft"),
            ("体系", "了解 IATF16949 体系五大工具，具备产品开发能力", "soft"),
            ("加分", "有卧加调试、五轴联动编程与操作经验", "bonus"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "工艺工程师（PE）",
        "target_count": 30,
        "min_education": "全日制本科",
        "min_years": 0,
        "english_required": "preferred",
        "english_threshold": "英语CET-4以上",
        "industry_required": [],
        "key_responsibilities": "生产现场新产品可生产性评估、试产跟进、失效模式分析、新工艺/材料/技术研究、量产工艺改善",
        "core_skills": ["生产可行性评估", "试产跟进", "FMEA", "工艺改善", "数据分析"],
        "extraction_focus": "生产可行性评估、试产跟进、失效模式分析、量产工艺改善、动手实践能力",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械、材料、智能制造等相关专业", "hard"),
            ("英语", "英语CET-4以上", "hard"),
            ("能力", "良好的沟通表达能力，逻辑思维清晰，善于数据分析", "soft"),
            ("现场", "能适应现场工作环境，动手实践能力强", "soft"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "压铸模具设计工程师",
        "target_count": 25,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "preferred",
        "english_threshold": "英语CET-4以上，能看懂或制作英文图纸",
        "industry_required": ["汽车零部件"],
        "key_responsibilities": "模具可行性分析与结构设计、压铸工艺参数确定、3D建模与模流分析、模具技术规格书、试模方案设计",
        "core_skills": ["UG12.0", "AutoCAD", "模流分析", "模具结构设计", "铝合金压铸模具"],
        "extraction_focus": "压铸模具设计经验、UG12.0熟练度、模流分析、铝合金压铸模具、2500T/3500T模具经验",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上，机械设计制造及自动化、模具类相关专业优先", "hard"),
            ("英语", "英语CET-4以上，能看懂或制作英文图纸", "hard"),
            ("年限", "汽车零部件行业工作经验≥3年", "hard"),
            ("行业", "熟悉铝合金压铸模具结构及压铸生产工艺，有压铸不良改善经验", "soft"),
            ("软件", "熟练应用 UG12.0/CAD 进行模具全3D设计", "soft"),
            ("加分", "有汽车零部件压铸模具设计经验，特别是2500T/3500T模具经验", "bonus"),
        ],
    },
    # ─────────────────────────────────────────────
    {
        "name": "非标自动化工程师",
        "target_count": 20,
        "min_education": "全日制本科",
        "min_years": 3,
        "english_required": "no",
        "industry_required": [],
        "key_responsibilities": "自动化项目可行性评估、厂商评估方案、审图、设备安装调试验收、非标自动化设备开发与维护",
        "core_skills": ["非标自动化", "设备开发", "设备调试", "项目评估"],
        "extraction_focus": "非标自动化设备设计经验、设备调试、项目可行性评估、技术方案制定",
        "rules": [
            ("学历", "第一学历必须为全日制本科及以上", "hard"),
            ("年限", "非标自动化设备设计经验≥3年", "hard"),
            ("能力", "良好的逻辑思维能力和问题解决能力", "soft"),
            ("协作", "较强的沟通协调能力和团队合作精神", "soft"),
        ],
    },
]


# =============================================================================
# Prompt 模板（结构化数据驱动版）
# =============================================================================

PROMPT_TEMPLATE = """# 角色：{job_name}岗位筛选官

你的工作：基于结构化简历 JSON 数据，对候选人进行评判。
绝不允许编造，所有判断必须基于 JSON 中的原文证据。

# 岗位关键信息

**核心职责**：{key_responsibilities}

**核心要求**：{core_skills}

# 评判规则（按硬性 → 软性 → 加分顺序）

{rules_section}

# 评判逻辑

1. 检查所有 `hard`（硬性）规则。**任一硬性规则不通过 → has_hard_fail=true → verdict='排除'**
2. 全部硬性规则通过的前提下，检查 `soft`（软性）规则：
   - 全部软性规则符合 → verdict='深绿'
   - 仅有1条软性规则不符合（且非严重）→ verdict='蓝色'
   - 多条软性规则不符合或有边界争议 → verdict='黄色'
3. `bonus`（加分）规则只增加亮点，不影响主判定，但可作为 pros 列出

# 输出格式（严格 JSON）

```json
{{
  "pros": [
    "✅ <一条具体的符合点，必须引用结构化简历中的原文证据，如「在XX公司任XX，原文：『...』」>",
    "..."
  ],
  "cons": [
    "❌ <一条具体的不符合点，必须引用证据或明确指出缺失>",
    "..."
  ],
  "has_hard_fail": true/false,
  "verdict": "深绿"/"蓝色"/"黄色"/"排除",
  "verdict_reason": "<一句话说明为什么是这个verdict>"
}}
```

# 重要约束

- pros 和 cons 中的每一条必须**有原文锚点**（公司名/时间段/原话引用）
- 如果某项无证据可循，写在 cons 中：「简历未体现XX」
- 不要使用主观评价词（"较强""不错""一般"），只陈述事实
- pros 和 cons 一般各 2-5 条，太多会让 HR 难以聚焦
- 严禁输出总分、百分比、Tier 档位、推荐程度等无意义标签
"""


def render_rules_section(rules):
    """把规则列表渲染成 Markdown"""
    type_tag = {
        "hard": "🔴 硬性",
        "soft": "🟡 软性",
        "bonus": "🟢 加分",
    }
    lines = []
    for i, (name, desc, kind) in enumerate(rules, 1):
        lines.append(f"## 规则{i}（{type_tag[kind]}）：{name}\n{desc}\n")
    return "\n".join(lines)


def render_prompt(job_def):
    return PROMPT_TEMPLATE.format(
        job_name=job_def["name"],
        key_responsibilities=job_def.get("key_responsibilities", ""),
        core_skills="、".join(job_def.get("core_skills", [])),
        rules_section=render_rules_section(job_def["rules"]),
    )


# =============================================================================
# 写入文件 + 数据库
# =============================================================================

def safe_dirname(name):
    """处理岗位名中的特殊字符（如『/』）"""
    return name.replace("/", "-").replace("\\", "-")


def init_all_jobs():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    jobs_dir = os.path.join(base_dir, "job_configs")
    os.makedirs(jobs_dir, exist_ok=True)

    database.init_db()

    for job_def in JOBS:
        name = job_def["name"]
        dir_name = safe_dirname(name)
        job_dir = os.path.join(jobs_dir, dir_name)
        os.makedirs(job_dir, exist_ok=True)

        # 写 config.json
        config = {
            "name": name,
            "target_count": job_def.get("target_count", 30),
            "min_education": job_def["min_education"],
            "min_years": job_def["min_years"],
            "english_required": job_def["english_required"],
            "english_threshold": job_def.get("english_threshold", ""),
            "industry_required": job_def.get("industry_required", []),
            "industry_excluded": job_def.get("industry_excluded", []),
            "requires_management": job_def.get("requires_management", False),
            "management_years": job_def.get("management_years", 0),
            "key_responsibilities": job_def.get("key_responsibilities", ""),
            "core_skills": job_def.get("core_skills", []),
            "preferred_skills": job_def.get("preferred_skills", []),
            "extraction_focus": job_def.get("extraction_focus", ""),
            "rules": [
                {"name": r[0], "description": r[1], "type": r[2]}
                for r in job_def["rules"]
            ],
        }
        with open(os.path.join(job_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        # 写 prompt.txt
        prompt_text = render_prompt(job_def)
        with open(os.path.join(job_dir, "prompt.txt"), "w", encoding="utf-8") as f:
            f.write(prompt_text)

        # 写入数据库
        database.upsert_job(name, config, prompt_text)
        print(f"  ✅ {name}")

    print(f"\n共初始化 {len(JOBS)} 个岗位配置。")


if __name__ == "__main__":
    init_all_jobs()
