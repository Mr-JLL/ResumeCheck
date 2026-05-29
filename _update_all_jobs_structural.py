# -*- coding: utf-8 -*-
"""
Structural changes: A1-A6, B1, B4, delete id=15
Run once, then delete.
"""
import sqlite3, sys, json
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
DB = "data/resumes.db"
NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def load(conn, jid):
    row = conn.execute("SELECT config_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise ValueError(f"job id={jid} not found")
    return json.loads(row[0])


def save(conn, jid, cfg):
    conn.execute(
        "UPDATE jobs SET config_json=?, updated_at=? WHERE id=?",
        (json.dumps(cfg, ensure_ascii=False), NOW, jid),
    )


def find_rule(cfg, name):
    for r in cfg["rules"]:
        if r["name"] == name:
            return r
    return None


conn = sqlite3.connect(DB)

# ── A2: Hard delete id=15 量产项目工程师 ────────────────────────────────
conn.execute("DELETE FROM evaluations WHERE job_id=15")
conn.execute("DELETE FROM jobs WHERE id=15")
print("A2: deleted id=15 量产项目工程师 (and evaluations)")

# ── A1: id=7 生产项目主管 — 管理: bonus → hard ──────────────────────────
cfg7 = load(conn, 7)
mgmt = find_rule(cfg7, "管理")
if mgmt:
    mgmt["type"] = "hard"
    print("A1: id=7 管理 type bonus→hard")
else:
    print("A1 WARNING: id=7 '管理' rule not found")
save(conn, 7, cfg7)

# ── A3: id=3 质量经理-主管 — expand industry_required ───────────────────
cfg3 = load(conn, 3)
cfg3["industry_required"] = ["汽车", "汽车零部件", "压铸", "铸造", "冲压", "注塑", "成型"]
# Also update 年限 rule description to reflect expanded industry
yr3 = find_rule(cfg3, "年限")
if yr3:
    yr3["description"] = "汽车行业压铸/铸造/冲压/注塑/成型相关产品质量经验>=8年"
    print("A3: id=3 industry_required expanded; 年限 desc updated")
save(conn, 3, cfg3)

# ── A4: id=11 工艺工程师PE — min_years 0→2, add 年限 hard rule ──────────
cfg11 = load(conn, 11)
cfg11["min_years"] = 2
# Add 年限 hard rule at front if not already present
if not find_rule(cfg11, "年限"):
    cfg11["rules"].insert(0, {
        "name": "年限",
        "description": "工艺工程师相关岗位工作经验>=2年",
        "type": "hard",
        "evidence_hint": (
            "【最低门槛】须在工艺/制造/生产技术类岗位累计>=2年，须写出公司和起止时间\n"
            "【不构成证据的表述】仅写公司名未写岗位性质；实习经历；年限不足2年\n"
            "【强证据示例】写出公司名+起止时间+工艺工程师/制造工程师职责，可计算出>=2年"
        ),
    })
    print("A4: id=11 min_years→2, added 年限 hard rule")
else:
    print("A4 WARNING: id=11 '年限' rule already exists — check manually")
save(conn, 11, cfg11)

# ── A5: id=12 压铸模具设计工程师 — min_years 3→2 ────────────────────────
cfg12 = load(conn, 12)
cfg12["min_years"] = 2
print("A5: id=12 min_years→2")
save(conn, 12, cfg12)

# ── A6: id=5 体系工程师 — add industry_required + add 行业 hard rule ─────
cfg5 = load(conn, 5)
cfg5["industry_required"] = ["汽车", "汽车零部件", "制造业"]
if not find_rule(cfg5, "行业"):
    cfg5["rules"].append({
        "name": "行业",
        "description": "须在汽车整车/汽车零部件/制造业企业任职",
        "type": "hard",
        "evidence_hint": (
            "【最低门槛】须直接在汽车整车厂/汽车零部件企业/制造业企业任职\n"
            "【不构成证据的表述】纯服务业/咨询/贸易公司；非制造业体系认证顾问\n"
            "【强证据示例】明确写出在汽车零部件厂或制造型企业担任体系工程师/质量职位"
        ),
    })
    print("A6: id=5 industry_required added; 行业 hard rule added")
else:
    print("A6 WARNING: id=5 '行业' rule already exists — check manually")
save(conn, 5, cfg5)

# ── B1: Fix vague personality-trait soft conditions ─────────────────────

# id=7 生产项目主管 — 能力
cfg7b = load(conn, 7)
cap7 = find_rule(cfg7b, "能力")
if cap7:
    cap7["description"] = "有跨部门协调生产资源的具体案例（含协调部门/问题类型/结果）"
    print("B1: id=7 能力 desc updated")
save(conn, 7, cfg7b)

# id=13 非标自动化工程师 — 能力 & 协作
cfg13 = load(conn, 13)
cap13 = find_rule(cfg13, "能力")
if cap13:
    cap13["description"] = "有非标设备设计难题攻关经历（含问题描述/解决方案/结果）"
    print("B1: id=13 能力 desc updated")
coop13 = find_rule(cfg13, "协作")
if coop13:
    coop13["description"] = "有与机械/电气/客户跨部门协调的具体案例"
    print("B1: id=13 协作 desc updated")
save(conn, 13, cfg13)

# id=14 PMC — 沟通协调
cfg14 = load(conn, 14)
comm14 = find_rule(cfg14, "沟通协调")
if comm14:
    comm14["description"] = "有生产计划冲突或物料异常时跨部门协调的具体案例"
    print("B1: id=14 沟通协调 desc updated")
save(conn, 14, cfg14)

# ── B4: Year floor = 2 ───────────────────────────────────────────────────
# id=2 市场内务: min_years 1→2, 年限 rule ≥1→≥2
cfg2 = load(conn, 2)
cfg2["min_years"] = 2
yr2 = find_rule(cfg2, "年限")
if yr2:
    yr2["description"] = "外贸跟单/采购经验>=2年（最好>=3年），或汽车外贸经验"
    print("B4: id=2 min_years→2, 年限 desc updated")
save(conn, 2, cfg2)

# id=14 PMC: min_years 1→2, 年限 rule ≥1→≥2
cfg14b = load(conn, 14)
cfg14b["min_years"] = 2
yr14 = find_rule(cfg14b, "年限")
if yr14:
    yr14["description"] = "生产计划或物料控制相关工作经验>=2年"
    print("B4: id=14 min_years→2, 年限 desc updated")
save(conn, 14, cfg14b)

# ── id=6 项目工程师 — update soft conditions to cover NPI + MP phases ────
cfg6 = load(conn, 6)
prj = find_rule(cfg6, "项目")
if prj:
    prj["description"] = "须作为项目Owner主导项目全流程（新产品导入或量产阶段均可）"
    print("id=6 项目 desc updated to cover NPI+MP")
flow = find_rule(cfg6, "流程")
if flow:
    flow["description"] = "熟悉IATF16949及五大工具，熟悉汽车产品开发或量产项目管理流程"
    print("id=6 流程 desc updated to cover NPI+MP")
cap6 = find_rule(cfg6, "能力")
if cap6:
    cap6["description"] = "能进行项目节点管理、跨部门协调及质量/交付问题跟踪"
    print("id=6 能力 desc updated to cover NPI+MP")
save(conn, 6, cfg6)

conn.commit()
conn.close()
print("\n=== Structural changes committed ===")
