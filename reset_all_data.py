"""
全量数据重置脚本
================
清除所有简历文件和数据库记录，保留岗位配置（jobs 表）。
运行前请确保已关闭 launcher.py / app.py。

使用方法：
    python reset_all_data.py
"""

import os
import sys
import glob
import shutil
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data", "resumes.db")

# 清除顺序须尊重外键依赖（子表先删）
TABLES_TO_CLEAR = [
    "talent_pool",
    "hr_stage_tags",
    "rejection_tags",
    "outcomes",
    "preference_signals",
    "preference_rules",
    "evaluations",
    "prefilter_rejects",
    "scrape_sessions",
    "candidates",
]

# data/ 下需要清空内容的子目录（删除其中所有文件，保留目录本身）
DATA_FILE_DIRS = [
    os.path.join(ROOT, "data", "resume_files"),  # 下载的 PDF/Word
    os.path.join(ROOT, "data", "dl_tmp"),         # 临时下载缓存
    os.path.join(ROOT, "data", "upload_tmp"),      # 临时上传缓存
]

# 整个目录直接删除再重建的目录
DATA_RMTREE_DIRS = [
    os.path.join(ROOT, "data", "chroma"),         # 向量数据库
]


# ── 统计 ──────────────────────────────────────────────────────


def collect_workspace_dirs():
    """找到所有 工作区_* 目录"""
    result = []
    for entry in os.scandir(ROOT):
        if entry.is_dir() and entry.name.startswith("工作区_"):
            result.append(entry.path)
    return result


def count_workspace_files(ws_dirs):
    total = 0
    rows = []
    for ws in ws_dirs:
        n = sum(1 for _ in glob.glob(os.path.join(ws, "**", "*"), recursive=True)
                if os.path.isfile(_))
        rows.append((os.path.relpath(ws, ROOT), n))
        total += n
    return rows, total


def count_data_files():
    total = 0
    rows = []
    for d in DATA_FILE_DIRS + DATA_RMTREE_DIRS:
        if not os.path.isdir(d):
            continue
        n = sum(1 for _ in glob.glob(os.path.join(d, "**", "*"), recursive=True)
                if os.path.isfile(_))
        rows.append((os.path.relpath(d, ROOT), n))
        total += n
    return rows, total


def get_db_counts(conn):
    counts = {}
    for table in TABLES_TO_CLEAR:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = row[0]
        except Exception:
            counts[table] = "（表不存在）"
    return counts


# ── 删除 ──────────────────────────────────────────────────────


def delete_workspace_dirs(ws_dirs):
    deleted = 0
    for ws in ws_dirs:
        try:
            shutil.rmtree(ws)
            deleted += 1
        except Exception as e:
            print(f"  警告：删除 {os.path.basename(ws)} 失败: {e}")
    return deleted


def clear_data_file_dirs():
    deleted = 0
    for d in DATA_FILE_DIRS:
        if not os.path.isdir(d):
            continue
        for fpath in glob.glob(os.path.join(d, "*")):
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
                    deleted += 1
                elif os.path.isdir(fpath):
                    shutil.rmtree(fpath)
                    deleted += 1
            except Exception as e:
                print(f"  警告：删除失败 {fpath}: {e}")
    return deleted


def delete_rmtree_dirs():
    for d in DATA_RMTREE_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            shutil.rmtree(d)
        except Exception as e:
            print(f"  警告：删除 {os.path.relpath(d, ROOT)} 失败: {e}")


def clear_tables(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in TABLES_TO_CLEAR:
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception as e:
            print(f"  警告：清空 {table} 失败: {e}")
    for table in TABLES_TO_CLEAR:
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        except Exception:
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("VACUUM")
    conn.commit()


# ── 主流程 ────────────────────────────────────────────────────


def main():
    print("=" * 58)
    print("  51job 全量数据重置")
    print("=" * 58)

    ws_dirs = collect_workspace_dirs()
    ws_rows, ws_total = count_workspace_files(ws_dirs)
    data_rows, data_total = count_data_files()

    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        db_counts = get_db_counts(conn)
    else:
        conn = None
        db_counts = {}

    print("\n── 即将删除的内容 ──────────────────────────────────────")

    print("\n[工作区目录（整个目录删除）]")
    if ws_rows:
        for rel, n in ws_rows:
            print(f"  {rel}  ({n} 个文件)")
    else:
        print("  （未找到工作区目录）")
    print(f"  合计：{ws_total} 个文件，{len(ws_dirs)} 个目录")

    print("\n[data/ 下的文件]")
    if data_rows:
        for rel, n in data_rows:
            print(f"  {rel}  ({n} 个文件)")
    else:
        print("  （无需清除的文件）")
    print(f"  合计：{data_total} 个文件")

    print(f"\n[数据库表]  {os.path.relpath(DB_PATH, ROOT)}")
    if db_counts:
        for table, cnt in db_counts.items():
            print(f"  {table:<25} {cnt} 条")
    else:
        print("  （数据库不存在，跳过）")

    print("\n[保留内容]")
    print("  jobs 表（岗位名称 / 规则 / AI Prompt）— 不动")

    print("\n" + "=" * 58)
    print("警告：此操作不可逆，删除后无法恢复！")
    print("=" * 58)

    ans = input("\n确认删除？输入 YES 继续，其他任意键取消：").strip()
    if ans != "YES":
        print("已取消，未做任何修改。")
        if conn:
            conn.close()
        return

    print("\n── 执行中 ──────────────────────────────────────────────")

    n = delete_workspace_dirs(ws_dirs)
    print(f"  ✓ 已删除 {n} 个工作区目录")

    n = clear_data_file_dirs()
    print(f"  ✓ 已清空 data/ 文件目录（{n} 项）")

    delete_rmtree_dirs()
    print(f"  ✓ 已删除向量数据库（data/chroma/）")

    if conn:
        clear_tables(conn)
        conn.close()
        print(f"  ✓ 已清空数据库表（共 {len(TABLES_TO_CLEAR)} 张）")
        print(f"  ✓ 数据库已压缩（VACUUM）")
    else:
        print("  ✓ 无数据库数据需要清除")

    print("\n── 完成 ─────────────────────────────────────────────────")
    print("所有简历数据已清空，岗位配置（jobs 表）已保留。")
    print("现在可以重新启动应用并开始新一轮抓取。")
    print("=" * 58)


if __name__ == "__main__":
    main()
