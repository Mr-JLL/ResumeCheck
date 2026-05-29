# -*- coding: utf-8 -*-
import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8")
conn = sqlite3.connect("data/resumes.db")
rows = conn.execute(
    "SELECT id, name, config_json FROM jobs WHERE id != 4 ORDER BY id"
).fetchall()
conn.close()
for jid, name, cfg_str in rows:
    cfg = json.loads(cfg_str)
    print(f"=== id={jid}  {name}  min_years={cfg.get('min_years')} ===")
    for r in cfg.get("rules", []):
        print(f"  [{r['type']}] {r['name']}")
        print(f"    desc: {r['description']}")
        print(f"    hint: {r.get('evidence_hint','(없음)')}")
    print()
