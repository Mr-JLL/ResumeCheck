"""
将现有Excel跟进表迁移到跟进台数据库。
名字匹配规则：去除空格后比对，同名认定为同一人，跳过迁移。
运行：python migrate_tracker.py [Excel路径]
"""
import sys, re, json, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')
import openpyxl
import database

EXCEL_PATH = sys.argv[1] if len(sys.argv) > 1 else '华阳精机面试跟进表第22周.xlsx'

STAGE_MAP = {
    '已入职':'已入职','待入职':'待入职','已一面':'已一面','已二面':'已二面',
    '签批中':'签批中','待谈薪':'待谈薪','谈薪中':'待谈薪',
    '不合适':'不合适','个人放弃':'个人放弃',
}

def norm(name):
    if not name: return ''
    return re.sub(r'\s+', '', str(name)).strip()

def main():
    database.init_db()
    database.run_startup_data_migrations()

    # 读取系统中已有通过候选人姓名（规范化）
    with database.get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT c.name
            FROM candidates c
            JOIN evaluations e ON e.candidate_id = c.id
            JOIN outcomes o ON o.evaluation_id = e.id
            WHERE o.action IN ('approved','hired')
        """).fetchall()
    sys_names = {norm(r['name']) for r in rows if r['name']}

    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb.active

    # 找数据行（跳过标题行，从序号=数字的行开始）
    headers = None
    results = {'matched':[], 'imported':[], 'skipped':[]}

    for row in ws.iter_rows(values_only=True):
        # 标题行检测
        if row[0] == '序号' or row[0] == 'NO':
            headers = row
            continue
        if not headers:
            continue
        if not isinstance(row[0], int):
            continue

        # 按位置读取：序号,状态,姓名,应聘岗位,年龄,面试时间,需求部门,学历,简历来源,面试结果,毕业院校/专业
        try:
            name = norm(row[2])
            if not name:
                results['skipped'].append(f"空姓名行 #{row[0]}")
                continue

            if name in sys_names:
                results['matched'].append(f"{row[2]}（已在系统中）")
                continue

            # 解析阶段
            raw_stage = str(row[9] or row[1] or '').strip()
            stage = STAGE_MAP.get(raw_stage, raw_stage)

            # 解析时间
            stage_time = ''
            if row[5]:
                stage_time = str(row[5]).strip()

            # 入职时间
            join_date = ''
            if len(row) > 12 and row[12]:
                try:
                    import datetime
                    d = row[12]
                    if isinstance(d, datetime.datetime):
                        join_date = d.strftime('%Y-%m-%d')
                    elif isinstance(d, (int, float)):
                        import openpyxl.utils.datetime as oxl_dt
                        join_date = oxl_dt.from_excel(d).strftime('%Y-%m-%d')
                except Exception:
                    pass

            if stage == '已入职' and join_date:
                stage_time = join_date

            entry_id = database.add_tracker_manual(
                name=str(row[2] or '').strip(),
                job_name=str(row[3] or '').strip(),
                age=int(row[4]) if isinstance(row[4], (int,float)) else None,
                stage=stage,
                stage_time=stage_time,
                department=str(row[6] or '').strip(),
                education=str(row[7] or '').strip(),
                school_major=str(row[10] or '').strip(),
                source_label=str(row[8] or '').strip(),
                entry_source='migrated',
            )
            results['imported'].append(f"{row[2]} → {stage} (id={entry_id})")
        except Exception as ex:
            results['skipped'].append(f"行 #{row[0]} 错误: {ex}")

    print(f"\n=== 迁移结果 ===")
    print(f"系统中已有（跳过）: {len(results['matched'])}人")
    for n in results['matched']: print(f"  ✓ {n}")
    print(f"成功导入: {len(results['imported'])}人")
    for n in results['imported']: print(f"  + {n}")
    print(f"跳过/错误: {len(results['skipped'])}")
    for n in results['skipped']: print(f"  ! {n}")

if __name__ == '__main__':
    main()
