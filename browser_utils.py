"""
browser_utils.py — 浏览器工具函数（app.py 与 node_agent.py 共用）
"""

import os
import json
import glob
import time


def _find_edge_exe():
    """查找本机 Edge 可执行文件路径（Windows）"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        )
        val = winreg.QueryValue(key, None)
        if val and os.path.exists(val):
            return val
    except Exception:
        pass
    return None


def _write_profile_prefs(profile_dir, download_dir):
    """首次创建 Edge Profile 时写入下载偏好，避免每次 PDF 弹出另存对话框"""
    pref_path = os.path.join(profile_dir, "Default", "Preferences")
    if not os.path.exists(pref_path):
        os.makedirs(os.path.dirname(pref_path), exist_ok=True)
        prefs = {
            "download": {
                "default_directory": download_dir.replace("\\", "/"),
                "prompt_for_download": False,
                "directory_upgrade": True,
            },
            "plugins": {"always_open_pdf_externally": True},
            "safebrowsing": {"enabled": False},
        }
        with open(pref_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f)


def _snapshot_dir(directory):
    """返回目录内当前文件集合（快照）"""
    return set(glob.glob(os.path.join(directory, "*")))


def _wait_for_new_file(directory, before_set, timeout=15):
    """等待目录中出现一个新的、完整下载的文件，返回文件路径或 None"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = set(glob.glob(os.path.join(directory, "*")))
        new_files = current - before_set
        # 排除浏览器未完成的临时文件
        complete = [f for f in new_files
                    if not f.endswith(".crdownload") and not f.endswith(".tmp")]
        if complete:
            return complete[0]
        time.sleep(0.8)
    return None
