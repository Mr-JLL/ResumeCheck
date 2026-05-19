"""
LLM 客户端初始化（精简版）
======================
只保留 DeepSeek 客户端的创建逻辑。
旧版本中的蒸馏、判断、白名单匹配、证据校验、最终判定等函数全部移除，
对应能力已分散到 extractor.py / judger.py / prefilter.py。
"""

import os
import logging
import httpx
from openai import OpenAI
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def initialize_client():
    """初始化 DeepSeek API 客户端。读取 .env 中的 DEEPSEEK_API_KEY。"""
    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        logger.error("未找到 DEEPSEEK_API_KEY，请检查 .env 文件")
        return None
    http_client = httpx.Client(trust_env=False, timeout=90.0)
    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        http_client=http_client,
    )


if __name__ == "__main__":
    c = initialize_client()
    print("Client OK" if c else "Client failed")
