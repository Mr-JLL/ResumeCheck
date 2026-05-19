"""
51job 抓取代理 · node_agent.py
=================================
在各设备上运行，控制本机浏览器（Edge/Chrome 自动识别），
将抓取到的简历 HTML 上传至中央服务器；服务器负责 LLM 评估和存储。

使用：
  1. 编辑 agent_config.json（首次运行会自动创建模板）
  2. 双击 start_agent.bat 立即启动，或运行 install_agent.bat 注册开机自启
"""

import os
import sys
import json
import time
import glob
import shutil
import base64
import socket
import random
import logging
import threading
import datetime
import subprocess
import concurrent.futures

import requests

# ── 编码 ──────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)

# 简历文件临时下载目录（PDF/Word 下载完成后上传服务器，本地副本随即删除）
DOWNLOAD_TMP = os.path.join(ROOT, "data", "dl_tmp")
os.makedirs(DOWNLOAD_TMP, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [agent] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(ROOT, "logs", "agent.log"),
            encoding="utf-8", mode="a",
        ),
    ],
)
logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────

CONFIG_FILE = os.path.join(ROOT, "agent_config.json")
_CONFIG_TEMPLATE = {
    "server_url": "http://localhost:5000",
    "device_name": socket.gethostname(),
    "browser": "auto",
}


def _load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_CONFIG_TEMPLATE, f, ensure_ascii=False, indent=2)
        logger.info(f"已生成配置文件：{CONFIG_FILE}，请按需修改后重启代理")
    cfg = dict(_CONFIG_TEMPLATE)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception as e:
        logger.warning(f"读取配置失败，使用默认值: {e}")
    return cfg


CFG = _load_config()
SERVER_URL = CFG["server_url"].rstrip("/")
DEVICE_NAME = CFG.get("device_name") or socket.gethostname()
DEVICE_ID = socket.gethostname().lower().replace(" ", "-")
BROWSER_PREF = CFG.get("browser", "auto").lower()

# ── 浏览器状态 ──────────────────────────────────────────────

_driver = None
_driver_lock = threading.Lock()
_browser_process = None       # subprocess.Popen 启动的浏览器进程
_prewarm_driver_future = None # concurrent.futures.Future，预热 EdgeDriver 路径
_scraping = False
_stop_flag = False
_current_job = None

# Edge 专用 Profile 目录（保存登录态 Cookie）及远程调试端口
EDGE_PROFILE_DIR = os.path.join(ROOT, "data", "edge_profile")
os.makedirs(EDGE_PROFILE_DIR, exist_ok=True)
EDGE_DEBUG_PORT = 19222


def _driver_alive():
    """浏览器是否处于开启状态（WebDriver 已连接 或 subprocess 进程仍在运行）"""
    global _driver
    if _driver:
        try:
            _ = _driver.title
            return True
        except Exception:
            _driver = None
    if _browser_process and _browser_process.poll() is None:
        return True
    return False


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


def _attach_driver_background_agent():
    """后台线程：等待 Edge CDP 端口就绪后附着 WebDriver"""
    global _driver
    import urllib.request as _urllib_req

    # 等待 Edge 调试端口就绪，最多 10 秒
    for _ in range(20):
        try:
            _urllib_req.urlopen(f"http://127.0.0.1:{EDGE_DEBUG_PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        logger.warning(f"[附着] CDP 端口 {EDGE_DEBUG_PORT} 等待超时，WebDriver 绑定失败")
        return

    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options

        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{EDGE_DEBUG_PORT}")

        drv = None
        if _prewarm_driver_future:
            try:
                browser_type, driver_path = _prewarm_driver_future.result(timeout=60)
                if browser_type == "edge" and driver_path:
                    from selenium.webdriver.edge.service import Service
                    drv = webdriver.Edge(service=Service(driver_path), options=opts)
            except Exception as e:
                logger.debug(f"[附着] 使用预热路径失败: {e}")

        if drv is None:
            drv = webdriver.Edge(options=opts)

        # attach 模式下 prefs 无效；通过 CDP 补设下载行为
        try:
            drv.execute_cdp_cmd("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": DOWNLOAD_TMP,
                "eventsEnabled": True,
            })
        except Exception:
            pass

        with _driver_lock:
            _driver = drv
        logger.info(f"✓ [附着] WebDriver 已绑定到 Edge（端口 {EDGE_DEBUG_PORT}）")
    except Exception as e:
        logger.error(f"[附着] WebDriver 绑定失败: {e}")


def _open_browser(browser_hint=None):
    """打开本机浏览器：subprocess 瞬时启动 Edge，后台绑定 WebDriver。
    Edge 不可用时回退到原始 Selenium 方式。"""
    global _browser_process

    edge_exe = _find_edge_exe()
    if not edge_exe:
        logger.warning("[打开浏览器] 未找到 Edge，使用 Selenium 直接启动（较慢）")
        return _open_browser_fallback(browser_hint)

    _write_profile_prefs(EDGE_PROFILE_DIR, DOWNLOAD_TMP)

    proc = subprocess.Popen([
        edge_exe,
        f"--remote-debugging-port={EDGE_DEBUG_PORT}",
        f"--user-data-dir={EDGE_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "https://ehire.51job.com",
    ])
    with _driver_lock:
        _browser_process = proc

    logger.info("✓ 浏览器已打开（subprocess），WebDriver 正在后台绑定...")
    threading.Thread(target=_attach_driver_background_agent, daemon=True).start()
    return True, "edge"


def _open_browser_fallback(browser_hint=None):
    """Edge 可执行文件找不到时的后备：原有 Selenium 方式，使用预热好的驱动路径"""
    global _driver
    pref = (browser_hint or BROWSER_PREF).lower()
    order = ["edge", "chrome"] if pref in ("auto", "edge") else ["chrome", "edge"]

    from selenium import webdriver
    dl_prefs = {
        "download.default_directory":         DOWNLOAD_TMP,
        "download.prompt_for_download":        False,
        "download.directory_upgrade":          True,
        "safebrowsing.enabled":                False,
        "plugins.always_open_pdf_externally":  True,
    }

    # 获取预热好的驱动路径
    prewarmed_type, prewarmed_path = None, None
    if _prewarm_driver_future:
        try:
            prewarmed_type, prewarmed_path = _prewarm_driver_future.result(timeout=60)
        except Exception:
            pass

    last_err = None
    for b in order:
        try:
            if b == "edge":
                from selenium.webdriver.edge.options import Options
                opts = Options()
                opts.add_argument("--disable-blink-features=AutomationControlled")
                opts.add_experimental_option("excludeSwitches", ["enable-automation"])
                opts.add_experimental_option("prefs", dl_prefs)
                if prewarmed_type == "edge" and prewarmed_path:
                    from selenium.webdriver.edge.service import Service
                    drv = webdriver.Edge(service=Service(prewarmed_path), options=opts)
                else:
                    try:
                        from webdriver_manager.microsoft import EdgeChromiumDriverManager
                        from selenium.webdriver.edge.service import Service
                        drv = webdriver.Edge(
                            service=Service(EdgeChromiumDriverManager().install()), options=opts)
                    except Exception:
                        drv = webdriver.Edge(options=opts)
            else:
                from selenium.webdriver.chrome.options import Options
                opts = Options()
                opts.add_argument("--disable-blink-features=AutomationControlled")
                opts.add_experimental_option("excludeSwitches", ["enable-automation"])
                opts.add_experimental_option("prefs", dl_prefs)
                if prewarmed_type == "chrome" and prewarmed_path:
                    from selenium.webdriver.chrome.service import Service
                    drv = webdriver.Chrome(service=Service(prewarmed_path), options=opts)
                else:
                    try:
                        from webdriver_manager.chrome import ChromeDriverManager
                        from selenium.webdriver.chrome.service import Service
                        drv = webdriver.Chrome(
                            service=Service(ChromeDriverManager().install()), options=opts)
                    except Exception:
                        drv = webdriver.Chrome(options=opts)

            drv.maximize_window()
            drv.get("https://ehire.51job.com")
            with _driver_lock:
                _driver = drv
            logger.info(f"✓ 浏览器已打开（{b}，Selenium 直接模式）")
            return True, b
        except Exception as e:
            last_err = e
            logger.warning(f"启动 {b} 失败: {e}")

    return False, str(last_err)


def _close_browser():
    global _driver, _browser_process
    with _driver_lock:
        drv = _driver
        proc = _browser_process
        _driver = None
        _browser_process = None
    if drv:
        try:
            drv.quit()
        except Exception:
            pass
    if proc:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    logger.info("浏览器已关闭")


# ── PDF / Word 下载辅助 ───────────────────────────────────────

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


def _try_download_files(driver, resume_id):
    """
    尝试在当前51job简历详情页点击下载，获取PDF和Word文件。
    完全异常安全：任何失败都静默跳过，不影响主抓取流程。
    返回 (pdf_path_or_None, word_path_or_None)
    """
    pdf_path = word_path = None
    try:
        before = _snapshot_dir(DOWNLOAD_TMP)

        # 用 JavaScript 查找并点击下载按钮（比 Selenium find_element 更稳定）
        click_main = """
        var candidates = Array.from(document.querySelectorAll(
            'button, a, [role="button"], [class*="download"]'));
        for (var el of candidates) {
            var txt = el.textContent.trim();
            var cls = el.className || '';
            if ((txt.includes('下载简历') || txt === '下载' ||
                 cls.includes('download') || cls.includes('Download')) &&
                el.offsetParent !== null) {
                el.click();
                return txt;
            }
        }
        return null;
        """
        clicked = driver.execute_script(click_main)
        if not clicked:
            return None, None

        time.sleep(1.5)  # 等待下拉菜单/弹窗出现

        # 点击 PDF 选项
        click_fmt = """
        var labels = arguments[0];
        var items = Array.from(document.querySelectorAll(
            'li, a, button, [role="option"], [role="menuitem"], [class*="item"]'));
        for (var el of items) {
            var txt = el.textContent.trim();
            if (el.offsetParent !== null) {
                for (var lbl of labels) {
                    if (txt.includes(lbl)) { el.click(); return txt; }
                }
            }
        }
        return null;
        """
        driver.execute_script(click_fmt, ["PDF", "pdf"])
        pdf_path = _wait_for_new_file(DOWNLOAD_TMP, before, timeout=15)
        if pdf_path:
            final = os.path.join(DOWNLOAD_TMP, resume_id + ".pdf")
            shutil.move(pdf_path, final)
            pdf_path = final
            before = _snapshot_dir(DOWNLOAD_TMP)

        # 点击 Word 选项（若下拉已关闭则重新触发）
        found_word = driver.execute_script(click_fmt, ["Word", "WORD", "word", "Doc"])
        if not found_word:
            driver.execute_script(click_main)
            time.sleep(1)
            driver.execute_script(click_fmt, ["Word", "WORD", "word", "Doc"])

        word_path = _wait_for_new_file(DOWNLOAD_TMP, before, timeout=15)
        if word_path:
            final = os.path.join(DOWNLOAD_TMP, resume_id + ".docx")
            shutil.move(word_path, final)
            word_path = final

    except Exception as e:
        logger.debug(f"下载文件失败 ({resume_id})，已跳过: {e}")

    return pdf_path, word_path


def _upload_file(resume_id, file_path, filetype):
    """将本地 PDF/Word 文件 base64 编码后上传服务器，成功后删除本地副本"""
    try:
        with open(file_path, "rb") as f:
            data_b64 = base64.b64encode(f.read()).decode("ascii")
        resp = requests.post(
            f"{SERVER_URL}/api/scrape/upload_file",
            json={"resume_id": resume_id, "filetype": filetype, "data_b64": data_b64},
            timeout=30,
        )
        return resp.ok
    except Exception as e:
        logger.debug(f"上传文件失败 ({resume_id}/{filetype}): {e}")
        return False
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass


# ── 抓取核心 ─────────────────────────────────────────────────

def _do_scrape(job_name: str, target_count: int, session_id: str):
    global _scraping, _stop_flag, _current_job

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import re

    # 等待 WebDriver 绑定就绪（subprocess 启动后后台绑定，最多等 30 秒）
    for _ in range(60):
        with _driver_lock:
            driver = _driver
        if driver:
            break
        if not (_browser_process and _browser_process.poll() is None):
            break  # 进程已退出或根本没有进程，直接报错
        time.sleep(0.5)

    if not driver:
        _send_progress(job_name, "error", 0, 0,
                       "浏览器未打开或 WebDriver 连接失败，请重新点击「打开浏览器」")
        return

    _scraping = True
    _stop_flag = False
    _current_job = job_name

    try:
        _send_progress(job_name, "scraping", 0, target_count, f"开始抓取，目标 {target_count} 份")

        # ── 第一阶段：收集候选人 ID ──
        task_pool, seen_ids, no_progress = [], set(), 0

        while len(task_pool) < target_count and no_progress < 10:
            if _stop_flag:
                break
            try:
                cards = driver.find_elements(
                    By.XPATH, "//div[contains(@class,'resume-card')]")
            except Exception:
                break

            found_new = False
            for card in cards:
                try:
                    html = card.get_attribute("innerHTML") or ""
                    m = re.search(r'no_interested_(\d+)', html)
                    rid = m.group(1) if m else None
                    if not rid or rid in seen_ids:
                        continue
                    name = "未知"
                    try:
                        ne = card.find_element(By.CLASS_NAME, "name")
                        raw = ne.text.split('\n')[0].strip()
                        name = re.sub(
                            r'(先生|女士|活跃|沟通|电话|拨打|离职|在职|刚刚|1周内|1小时|3日内|1个月内|\s)',
                            '', raw) or "未知"
                    except Exception:
                        pass
                    task_pool.append({"id": rid, "name": name})
                    seen_ids.add(rid)
                    found_new = True
                    if len(task_pool) >= target_count:
                        break
                except Exception:
                    continue

            _send_progress(job_name, "scraping", len(task_pool), target_count,
                           f"扫描到 {len(task_pool)}/{target_count} 份候选人")
            if len(task_pool) >= target_count:
                break
            try:
                if cards:
                    driver.execute_script("""
                        var last = arguments[0];
                        last.scrollIntoView({block: 'end', behavior: 'instant'});
                        var el = last.parentElement;
                        while (el && el !== document.body) {
                            var style = window.getComputedStyle(el);
                            if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                                el.scrollTop = el.scrollHeight;
                                return;
                            }
                            el = el.parentElement;
                        }
                        window.scrollTo(0, document.body.scrollHeight);
                    """, cards[-1])
            except Exception:
                pass
            wait_secs = min(2.5 + no_progress * 0.5, 5.0)
            time.sleep(wait_secs)
            no_progress = no_progress + 1 if not found_new else 0

        if not task_pool:
            _send_progress(job_name, "error", 0, 0,
                           "未找到候选人，请确认已在 51job 搜索结果页并已有候选人列表")
            return

        # ── 第二阶段：逐个打开详情页并上传 ──
        uploaded = 0
        for i, task in enumerate(task_pool):
            if _stop_flag:
                _send_progress(job_name, "stopped", i, len(task_pool), "已停止")
                break

            _send_progress(job_name, "scraping", i + 1, len(task_pool),
                           f"保存 ({i+1}/{len(task_pool)}): {task['name']}")
            try:
                driver.get(
                    "https://ehire.51job.com/Revision/talent/resume/detail"
                    f"?resumeId={task['id']}")
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "work")))
                html_content = driver.page_source

                resp = requests.post(
                    f"{SERVER_URL}/api/scrape/upload_html",
                    json={
                        "job_name": job_name,
                        "resume_id": task["id"],
                        "name_hint": task["name"],
                        "html": html_content,
                        "session_id": session_id,
                        "device_id": DEVICE_ID,
                        "device_name": DEVICE_NAME,
                        "index": i + 1,
                        "total": len(task_pool),
                    },
                    timeout=30,
                )
                if resp.ok:
                    uploaded += 1

            except Exception as e:
                logger.warning(f"  ⚠ {task['name']} 上传失败: {e}")

            time.sleep(random.uniform(2.0, 3.5))

        _send_progress(job_name, "done", uploaded, len(task_pool),
                       f"✓ 抓取完成，已上传 {uploaded}/{len(task_pool)} 份")
        logger.info(f"✓ [{job_name}] 抓取完成，上传 {uploaded} 份")

    except Exception as e:
        logger.exception(f"抓取异常: {e}")
        _send_progress(job_name, "error", 0, 0, str(e))
    finally:
        _scraping = False
        _current_job = None


def _send_progress(job_name, phase, current, total, msg=""):
    try:
        requests.post(
            f"{SERVER_URL}/api/agent/progress",
            json={
                "device_id": DEVICE_ID,
                "device_name": DEVICE_NAME,
                "job_name": job_name,
                "phase": phase,
                "current": current,
                "total": total,
                "message": msg,
                "browser_open": _driver_alive(),
            },
            timeout=5,
        )
    except Exception:
        pass


# ── 命令处理 ──────────────────────────────────────────────

def _handle(cmd: dict):
    global _stop_flag
    action = cmd.get("command", "")
    job_name = cmd.get("job_name", "")
    logger.info(f"← 命令: {action}" + (f"  [{job_name}]" if job_name else ""))

    if action == "open_browser":
        if _driver_alive():
            return
        threading.Thread(
            target=_open_browser,
            args=(cmd.get("browser"),),
            daemon=True,
        ).start()

    elif action == "close_browser":
        _close_browser()

    elif action == "scrape":
        if _scraping:
            logger.info("已在抓取中，忽略重复命令")
            return
        if not _driver_alive():
            _send_progress(job_name, "error", 0, 0, "浏览器未打开，请先点击「打开浏览器」")
            return
        session_id = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        target = int(cmd.get("target_count", 30))
        threading.Thread(
            target=_do_scrape,
            args=(job_name, target, session_id),
            daemon=True,
        ).start()

    elif action == "stop":
        _stop_flag = True

    elif action == "download_file":
        resume_id = cmd.get("resume_id", "")
        filetype  = cmd.get("filetype", "pdf")
        if not resume_id or not _driver_alive():
            return
        with _driver_lock:
            driver = _driver
        if not driver:
            return  # subprocess 在运行但 WebDriver 尚未绑定，跳过下载
        threading.Thread(
            target=_do_download_and_upload,
            args=(driver, resume_id, filetype),
            daemon=True,
        ).start()


def _do_download_and_upload(driver, resume_id, filetype):
    """按需下载：访问简历详情页，下载指定文件，上传服务器。"""
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        driver.get(
            f"https://ehire.51job.com/Revision/talent/resume/detail?resumeId={resume_id}")
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "work")))
        pdf_p, word_p = _try_download_files(driver, resume_id)
        if filetype == "pdf" and pdf_p:
            _upload_file(resume_id, pdf_p, "pdf")
        elif filetype == "word" and word_p:
            _upload_file(resume_id, word_p, "word")
        else:
            if pdf_p:  _upload_file(resume_id, pdf_p, "pdf")
            if word_p: _upload_file(resume_id, word_p, "word")
    except Exception as e:
        logger.warning(f"按需下载失败 ({resume_id}): {e}")


# ── 预热：agent 启动时后台解析驱动路径 ───────────────────────

def _prewarm_driver():
    global _prewarm_driver_future
    _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="prewarm-agent")

    def _resolve():
        try:
            from webdriver_manager.microsoft import EdgeChromiumDriverManager
            path = EdgeChromiumDriverManager().install()
            logger.info(f"[预热] EdgeDriver 路径已缓存: {path}")
            return ("edge", path)
        except Exception:
            pass
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            path = ChromeDriverManager().install()
            logger.info(f"[预热] ChromeDriver 路径已缓存: {path}")
            return ("chrome", path)
        except Exception as e:
            logger.info(f"[预热] 所有驱动预热失败（将使用 Selenium 内置管理）: {e}")
            return (None, None)

    _prewarm_driver_future = _ex.submit(_resolve)
    _ex.shutdown(wait=False)


_prewarm_driver()


# ── 主轮询循环 ────────────────────────────────────────────

def _poll():
    logger.info(f"代理已启动 · 设备：{DEVICE_NAME}（{DEVICE_ID}）· 服务器：{SERVER_URL}")
    consecutive_fail = 0

    while True:
        try:
            r = requests.get(
                f"{SERVER_URL}/api/agent/poll",
                params={
                    "device_id":   DEVICE_ID,
                    "device_name": DEVICE_NAME,
                    "browser_open": int(_driver_alive()),
                    "status":      "scraping" if _scraping else "idle",
                    "current_job": _current_job or "",
                    "long_poll":   "1",
                },
                timeout=30,   # 25s 服务器等待 + 5s 网络缓冲
            )
            if r.ok:
                consecutive_fail = 0
                for cmd in r.json().get("commands", []):
                    _handle(cmd)
            else:
                consecutive_fail += 1
                time.sleep(3)
        except requests.exceptions.ConnectionError:
            if consecutive_fail == 0:
                logger.warning(f"无法连接服务器 {SERVER_URL}，将持续重试…")
            consecutive_fail += 1
            time.sleep(5)
        except Exception as e:
            logger.debug(f"轮询异常: {e}")
            consecutive_fail += 1
            time.sleep(3)


if __name__ == "__main__":
    _poll()
