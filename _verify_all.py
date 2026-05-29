# -*- coding: utf-8 -*-
import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8")
DB = "data/resumes.db"
conn = sqlite3.connect(DB)

def load(jid):
    row = conn.execute("SELECT name, config_json FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        return None, None
    return row[0], json.loads(row[1])

def find_rule(cfg, name):
    for r in cfg.get("rules", []):
        if r["name"] == name:
            return r
    return None

def check(label, cond, detail=""):
    status = "OK" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" | {detail}" if detail else ""))
    return cond

all_ok = True

print("=== A2: id=15 deleted ===")
row15 = conn.execute("SELECT id FROM jobs WHERE id=15").fetchone()
ok = check("id=15 not in jobs", row15 is None)
ev15 = conn.execute("SELECT COUNT(*) FROM evaluations WHERE job_id=15").fetchone()[0]
ok2 = check("evaluations for id=15 = 0", ev15 == 0, f"count={ev15}")
all_ok = all_ok and ok and ok2

print("\n=== A1: id=7 管理 type=hard ===")
_, cfg7 = load(7)
r = find_rule(cfg7, "管理")
check("管理 exists", r is not None)
check("管理 type=hard", r and r["type"] == "hard", f"actual={r['type'] if r else 'N/A'}")

print("\n=== A3: id=3 industry_required expanded ===")
_, cfg3 = load(3)
ind3 = cfg3.get("industry_required", [])
for kw in ["铸造", "冲压", "注塑", "成型"]:
    check(f"industry has '{kw}'", kw in ind3)
yr3 = find_rule(cfg3, "年限")
check("年限 desc mentions 铸造/冲压", yr3 and ("铸造" in yr3["description"] or "冲压" in yr3["description"]))

print("\n=== A4: id=11 min_years=2, 年限 rule added ===")
_, cfg11 = load(11)
check("min_years=2", cfg11.get("min_years") == 2, f"actual={cfg11.get('min_years')}")
yr11 = find_rule(cfg11, "年限")
check("年限 rule exists", yr11 is not None)
check("年限 type=hard", yr11 and yr11["type"] == "hard")

print("\n=== A5: id=12 min_years=2 ===")
_, cfg12 = load(12)
check("min_years=2", cfg12.get("min_years") == 2, f"actual={cfg12.get('min_years')}")

print("\n=== A6: id=5 industry_required + 行业 hard rule ===")
_, cfg5 = load(5)
ind5 = cfg5.get("industry_required", [])
check("industry_required set", len(ind5) > 0, str(ind5))
r5 = find_rule(cfg5, "行业")
check("行业 rule exists", r5 is not None)
check("行业 type=hard", r5 and r5["type"] == "hard")

print("\n=== B1: vague soft conditions fixed ===")
_, cfg7b = load(7)
cap7 = find_rule(cfg7b, "能力")
check("id=7 能力 not vague", cap7 and "沟通" not in cap7["description"][:6],
      cap7["description"][:40] if cap7 else "N/A")

_, cfg13 = load(13)
cap13 = find_rule(cfg13, "能力")
check("id=13 能力 has concrete desc", cap13 and "攻关" in cap13["description"],
      cap13["description"][:40] if cap13 else "N/A")
coop13 = find_rule(cfg13, "协作")
check("id=13 协作 has concrete desc", coop13 and "跨部门" in coop13["description"],
      coop13["description"][:40] if coop13 else "N/A")

_, cfg14 = load(14)
comm14 = find_rule(cfg14, "沟通协调")
check("id=14 沟通协调 has concrete desc", comm14 and "案例" in comm14["description"],
      comm14["description"][:40] if comm14 else "N/A")

print("\n=== B4: year floor=2 ===")
_, cfg2 = load(2)
check("id=2 min_years=2", cfg2.get("min_years") == 2, f"actual={cfg2.get('min_years')}")
yr2 = find_rule(cfg2, "年限")
check("id=2 年限 desc has >=2", yr2 and ">=2" in yr2["description"], yr2["description"][:50] if yr2 else "N/A")
check("id=14 min_years=2", cfg14.get("min_years") == 2, f"actual={cfg14.get('min_years')}")
yr14 = find_rule(cfg14, "年限")
check("id=14 年限 desc has >=2", yr14 and ">=2" in yr14["description"], yr14["description"][:50] if yr14 else "N/A")

print("\n=== id=6 soft conditions cover NPI+MP ===")
_, cfg6 = load(6)
prj = find_rule(cfg6, "项目")
check("项目 desc mentions 新产品导入或量产", prj and "量产" in prj["description"],
      prj["description"][:60] if prj else "N/A")
flow = find_rule(cfg6, "流程")
check("流程 desc mentions 量产", flow and "量产" in flow["description"],
      flow["description"][:60] if flow else "N/A")

print("\n=== C-class: three-part hints present ===")
SAMPLE_CHECKS = [
    (1, "年限"), (2, "能力"), (3, "体系"), (5, "行业"), (6, "项目"),
    (7, "管理"), (8, "工艺"), (9, "电气"), (10, "体系"), (11, "现场"),
    (12, "行业"), (13, "能力"), (14, "沟通协调"),
]
for jid, rname in SAMPLE_CHECKS:
    _, cfg = load(jid)
    if cfg is None:
        print(f"  [FAIL] id={jid} not found")
        continue
    r = find_rule(cfg, rname)
    hint = r.get("evidence_hint", "") if r else ""
    has_all = all(k in hint for k in ["【最低门槛】", "【不构成证据的表述】", "【强证据示例】"])
    check(f"id={jid} {rname} three-part hint", has_all)

print("\n=== B2: 了解 hints corrected ===")
_, cfg9 = load(9)
e9 = find_rule(cfg9, "电气")
check("id=9 电气 hint allows 了解", e9 and "了解也构成证据" in e9.get("evidence_hint",""),
      (e9.get("evidence_hint","")[:50] if e9 else "N/A"))
_, cfg10 = load(10)
t10 = find_rule(cfg10, "体系")
check("id=10 体系 hint allows 了解", t10 and "了解也构成证据" in t10.get("evidence_hint",""),
      (t10.get("evidence_hint","")[:50] if t10 else "N/A"))

conn.close()
print("\n=== Verification complete ===")
