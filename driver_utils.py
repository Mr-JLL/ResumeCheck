import logging
import re
import os
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

def extract_resume_id(card_element):
    """从源码中抠取唯一 ID"""
    try:
        html = card_element.get_attribute("innerHTML")
        match = re.search(r'no_interested_(\d+)', html)
        return match.group(1) if match else None
    except: return None

def get_clean_name(card_element):
    """提取纯净姓名"""
    try:
        name_el = card_element.find_element(By.CLASS_NAME, "name")
        text = name_el.text.split('\n')[0].strip()
        # 过滤掉所有干扰词汇
        clean = re.sub(r'(先生|女士|活跃|沟通|电话|拨打|离职|在职|刚刚|1周内|1小时|3日内|1个月内|[\s])', '', text)
        return clean if clean else "未知"
    except: return "未知"

def save_page_to_local(driver, file_path):
    """将当前详情页完整源码保存到本地"""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        return True
    except Exception as e:
        logger.error(f"保存文件失败: {e}")
        return False