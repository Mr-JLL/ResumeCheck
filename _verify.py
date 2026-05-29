# -*- coding: utf-8 -*-
import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8")
conn = sqlite3.connect("data/resumes.db")
cfg = json.loads(conn.execute("SELECT config_json FROM jobs WHERE id=4").fetchone()[0])
conn.close()
print("=== evidence_hint 格式验证 ===")
for r in cfg["rules"]:
    hint = r["evidence_hint"]
    has_min = "【最低门槛】" in hint
    has_not = "【不构成证据的表述】" in hint
    has_strong = "【强证据示例】" in hint
    ok = "OK" if (has_min and has_not and has_strong) else "MISSING"
    print(f"  [{r['type']}] {r['name']}: {ok}  (最低门槛={has_min} 不构成={has_not} 强证据={has_strong})")

print()
print("=== prompt_text 接受背景验证 ===")
pt = conn = sqlite3.connect("data/resumes.db")
pt = sqlite3.connect("data/resumes.db").execute("SELECT prompt_text FROM jobs WHERE id=4").fetchone()[0]
print("含'接受背景':", "接受背景" in pt)
print("含'量产':", "量产" in pt)
print("含'新产品导入':", "新产品导入" in pt)
