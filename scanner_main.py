"""
scanner_main.py（命令行抓取兼容入口）
==================================
保留旧的命令行用法，但内部改为：
  - 使用统一的工作区命名（safe_filename）
  - 抓取完成后提示用户运行 analyzer_main.py 进行评估

推荐使用 Web 界面（双击 启动.bat 或运行 python launcher.py）的「抓取并评估」一键流。
"""

import os
import sys
import json
import time
import random
import logging
import argparse

os.environ['WDM_SSL_VERIFY'] = '0'
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import driver_utils
import database
import pipeline

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def run_scanner():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="岗位名称")
    parser.add_argument("--count", type=int, default=None,
                        help="目标抓取数量（默认读 config.json 中的 target_count）")
    args = parser.parse_args()

    job_record = database.get_job(args.name)
    if not job_record:
        print(f"❌ 找不到岗位 '{args.name}'，请先在 Web 界面创建或运行 init_jobs.py")
        return

    config = json.loads(job_record["config_json"])
    target_count = args.count if args.count else int(config.get("target_count", 30))

    workspace = f"工作区_{pipeline.safe_filename(args.name)}"
    html_dir = os.path.join(workspace, "简历原始文件")
    os.makedirs(html_dir, exist_ok=True)

    options = Options()
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    script_dir = os.path.dirname(os.path.abspath(__file__))
    driver_path = os.path.join(script_dir, "msedgedriver.exe")
    service = Service(executable_path=driver_path)
    driver = webdriver.Edge(service=service, options=options)
    driver.maximize_window()

    try:
        driver.get("https://ehire.51job.com")
        input(f"\n🔑 登录并搜出[{args.name}]的结果后，按回车开始（目标 {target_count} 份）...")

        # 阶段 1：收集 ID
        task_pool = []
        seen_ids = set()
        while len(task_pool) < target_count:
            cards = driver.find_elements(By.XPATH, "//div[contains(@class, 'resume-card')]")
            found_new = False
            for card in cards:
                rid = driver_utils.extract_resume_id(card)
                if rid and rid not in seen_ids:
                    name = driver_utils.get_clean_name(card)
                    task_pool.append({'id': rid, 'name': name})
                    seen_ids.add(rid)
                    found_new = True
            if len(task_pool) >= target_count:
                break
            if cards:
                driver.execute_script("arguments[0].scrollIntoView();", cards[-1])
            time.sleep(2.5)
            if not found_new:
                break
            logger.info(f"📡 已扫描到 {len(task_pool)} 人")

        # 阶段 2：保存详情页
        for i, task in enumerate(task_pool):
            file_name = f"{i+1:03d}_{task['name']}_{task['id']}.html"
            file_path = os.path.join(html_dir, file_name)
            if os.path.exists(file_path):
                continue
            logger.info(f"📥 ({i+1}/{len(task_pool)}) {task['name']}")
            driver.get(f"https://ehire.51job.com/Revision/talent/resume/detail?resumeId={task['id']}")
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "work")))
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
            except Exception as e:
                logger.warning(f"  保存失败: {e}")
            time.sleep(random.uniform(3.0, 5.0))

        logger.info(f"✅ [{args.name}] 抓取完成。")
        logger.info(f"   接下来运行：python analyzer_main.py --name \"{args.name}\"")
    finally:
        driver.quit()


if __name__ == "__main__":
    run_scanner()
