# -*- coding: utf-8 -*-
"""
临时脚本：更新质量工程师岗位的 config_json 和 prompt_text。
运行后可删除本文件。
"""
import sqlite3
import sys
import json
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = "data/resumes.db"

NEW_RULES = [
    {
        "name": "年限",
        "description": "在符合行业条件的企业质量岗位工作经验>=2年",
        "type": "hard",
        "evidence_hint": (
            "【最低门槛】须有在汽车/压铸/铸造/冲压/注塑/成型行业质量岗位的工作经历，"
            "通过起止时间或明确年限累计>=2年\n"
            "【不构成证据的表述】只写公司名不写年限；只写汽车行业经验但无质量岗位描述；实习经历\n"
            "【强证据示例】写出公司名+起止时间段+质量岗位职责，可计算出>=2年"
        ),
    },
    {
        "name": "行业",
        "description": "须同时满足：①在汽车整车/汽车零部件企业任职；②公司产品涉及压铸/铸造/冲压/注塑/成型工艺",
        "type": "hard",
        "evidence_hint": (
            "【最低门槛】须直接在汽车整车厂或汽车零部件企业任职，且公司产品属于压铸/铸造/冲压/注塑/成型任一工艺\n"
            "【不构成证据的表述】汽车行业但产品为软件/线束/橡胶等非金属成型工艺；一般机械制造/电子厂；供应链贸易公司\n"
            "【强证据示例】明确写出汽车铝合金压铸件厂质量工程师或汽车冲压零部件公司IATF16949体系"
        ),
    },
    {
        "name": "体系",
        "description": "熟悉IATF16949以及VDA6.3要求，有实际工作经历",
        "type": "soft",
        "evidence_hint": (
            "【最低门槛】须有IATF16949或VDA6.3实际工作经历，内审/外审/体系文件维护任一\n"
            "【不构成证据的表述】了解/熟悉等泛词；仅写知道该标准；参与但无具体工作描述\n"
            "【强证据示例】担任内审员并主持过内审；或配合客户完成VDA6.3过程审核"
        ),
    },
    {
        "name": "工具",
        "description": "掌握8D及PFMEA（必须），且有量产工具（SPC/MSA）或新产品工具（APQP/PPAP）至少一组实际应用经历",
        "type": "soft",
        "evidence_hint": (
            "【最低门槛】须有8D处理案例描述 + PFMEA使用描述，"
            "且有SPC/MSA（量产组）或APQP/PPAP（新产品组）中至少一组的具体项目描述\n"
            "【不构成证据的表述】仅列出工具名称；了解/会用等泛词；未写具体应用场景\n"
            "【强证据示例】描述了8D报告处理某客户投诉的具体过程；或描述了使用SPC监控关键尺寸的控制图分析"
        ),
    },
    {
        "name": "能力",
        "description": "能独立处理客户反馈并协调内部落实改善",
        "type": "soft",
        "evidence_hint": (
            "【最低门槛】须有独立处理客诉的场景描述，客诉类型/根因分析/改善措施三项中至少包含两项\n"
            "【不构成证据的表述】负责客诉处理等职责罗列；参与过等非独立表述；泛泛描述无具体案例\n"
            "【强证据示例】描述了具体客诉案例，包含问题描述+根因分析+整改措施的完整闭环"
        ),
    },
    {
        "name": "英语",
        "description": "英语CET-6以上，能作为工作语言（仅影响同色组内排序，不改变颜色）",
        "type": "bonus",
        "evidence_hint": (
            "【最低门槛】须有实际英语工作场景（技术文件阅读/客户书面对接/审核资料），仅写证书等级不满足\n"
            "【不构成证据的表述】英语良好等泛词；仅列CET-4/CET-6证书；会一些英语\n"
            "【强证据示例】描述了用英语处理客户审核资料、技术规范或跨国邮件沟通的具体场景"
        ),
    },
]

NEW_PROMPT_TEXT = (
    "# 岗位：质量工程师\n"
    "\n"
    "## 核心职责\n"
    "过程质量控制（新产品导入或量产阶段均可）、CPK/PPK分析、客诉8D处理、现场质量培训\n"
    "\n"
    "## 接受背景\n"
    "新产品导入背景（APQP/PPAP经历）和量产质量控制背景（SPC/MSA经历）均可接受，不作区分。\n"
    "行业须为汽车整车/汽车零部件，工艺须涉及压铸/铸造/冲压/注塑/成型之一。\n"
    "\n"
    "## 全局证据原则\n"
    "评判时所有条件均须在简历原文中找到对应证据，禁止主观推断：\n"
    "- **软性条件**：须有具体项目/数字/时间段支撑；熟悉/了解/参与等词汇不构成证据\n"
    "- **行业条件**：须直接在该行业企业任职；供应链上下游/一般机械制造不满足\n"
    "- **英语条件**：须有实际使用场景（客户沟通/外文资料/跨国项目）；仅写证书等级不满足\n"
    "- **工具/软件**：须有实际应用项目描述；了解/会用不满足\n"
)


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT config_json FROM jobs WHERE id=4")
    row = cur.fetchone()
    if not row:
        print("ERROR: job id=4 not found")
        conn.close()
        return

    config = json.loads(row[0])
    config["industry_required"] = ["汽车", "汽车零部件", "压铸", "铸造", "冲压", "注塑", "成型"]
    config["key_responsibilities"] = "过程质量控制（新产品导入或量产阶段均可）、CPK/PPK分析、客诉8D处理、现场质量培训"
    config["extraction_focus"] = (
        "IATF16949/VDA6.3实际经历、五大工具（重点：PFMEA/8D/SPC/MSA/APQP/PPAP）、"
        "客诉8D处理案例、CPK/PPK分析、工艺行业（压铸/铸造/冲压/注塑/成型）、英语工作场景"
    )
    config["rules"] = NEW_RULES

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    cur.execute(
        "UPDATE jobs SET config_json=?, prompt_text=?, updated_at=? WHERE id=4",
        (json.dumps(config, ensure_ascii=False), NEW_PROMPT_TEXT, now),
    )
    conn.commit()
    conn.close()

    # 验证回读
    conn2 = sqlite3.connect(DB_PATH)
    cur2 = conn2.cursor()
    cur2.execute("SELECT config_json, prompt_text FROM jobs WHERE id=4")
    cfg_str, pt = cur2.fetchone()
    conn2.close()
    cfg = json.loads(cfg_str)
    print("industry_required:", cfg["industry_required"])
    print("rules count:", len(cfg["rules"]))
    for r in cfg["rules"]:
        print(f"  [{r['type']}] {r['name']}: {r['description'][:50]}")
    print()
    print("prompt_text 前6行:")
    for line in pt.splitlines()[:8]:
        print(" ", line)
    print("\nOK")


if __name__ == "__main__":
    main()
