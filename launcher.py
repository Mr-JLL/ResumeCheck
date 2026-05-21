"""
一键启动器
=========
非技术人员双击运行：
1. 检查 Python 依赖
2. 初始化数据库（首次运行）
3. 启动 Flask 服务
4. 自动打开浏览器
"""

import os
import sys
import time
import socket
import subprocess
import webbrowser

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from utils import get_lan_ip

HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}"


REQUIRED_PACKAGES = [
    ("flask", "flask"),
    ("flask_cors", "flask-cors"),
    ("openai", "openai"),
    ("httpx", "httpx"),
    ("dotenv", "python-dotenv"),
    ("bs4", "beautifulsoup4"),
    ("markdownify", "markdownify"),
    ("selenium", "selenium"),
    ("webdriver_manager", "webdriver-manager"),
    ("openpyxl", "openpyxl"),
    ("chromadb", "chromadb"),
]


def check_and_install():
    missing = []
    for mod, pkg in REQUIRED_PACKAGES:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return True
    print(f"检测到缺失依赖：{', '.join(missing)}")
    print("正在自动安装...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing
        )
        print("✓ 安装完成")
        return True
    except Exception as e:
        print(f"✗ 安装失败：{e}")
        print("请手动运行：pip install " + " ".join(missing))
        return False


def port_open(port, timeout=0.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((HOST, port))
        s.close()
        return True
    except Exception:
        return False


def kill_port(port):
    """终止占用指定端口的进程（Windows）。返回是否成功找到并发送终止信号。"""
    killed = False
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    subprocess.run(
                        ["taskkill", "/PID", pid, "/F", "/T"],
                        capture_output=True, timeout=5)
                    killed = True
    except Exception:
        pass
    return killed


def init_jobs_if_empty():
    """如果数据库里还没有岗位，初始化13个默认岗位"""
    sys.path.insert(0, ROOT)
    import database
    database.init_db()
    jobs = database.list_jobs()
    if not jobs:
        print("首次运行，正在初始化13个岗位配置...")
        import init_jobs
        init_jobs.init_all_jobs()
        print("✓ 岗位初始化完成")


def check_env_file():
    """检查 .env 文件，缺失时自动创建模板"""
    env_path = os.path.join(ROOT, ".env")
    if not os.path.exists(env_path):
        print(f"⚠ 未找到 .env 文件，正在创建模板...")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("# DeepSeek API 密钥（必填）\n"
                    "# 在 https://platform.deepseek.com 获取\n"
                    "DEEPSEEK_API_KEY=请填写你的密钥\n")
        print(f"  已创建：{env_path}")
        print(f"  请打开此文件填入 DEEPSEEK_API_KEY 后重新启动。")
        input("按回车退出...")
        sys.exit(0)

    # 检查 key 是否填写
    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()
    if "请填写你的密钥" in content or "DEEPSEEK_API_KEY=" not in content:
        print(f"⚠ .env 中的 DEEPSEEK_API_KEY 还未填写。")
        print(f"  请编辑：{env_path}")
        input("按回车退出...")
        sys.exit(0)


def main():
    os.chdir(ROOT)

    print("=" * 60)
    print("  华阳精机简历筛选系统 · 启动中")
    print("=" * 60)

    if not check_and_install():
        input("按回车退出...")
        return

    check_env_file()
    init_jobs_if_empty()

    # 启动 Flask（若有旧进程则先杀掉）
    if port_open(PORT):
        print(f"检测到端口 {PORT} 有旧进程，正在终止...")
        kill_port(PORT)
        for _ in range(8):          # 最多等 4 秒让端口释放
            time.sleep(0.5)
            if not port_open(PORT):
                break
        else:
            print(f"⚠ 端口 {PORT} 仍被占用，无法启动。请手动关闭占用程序后重试。")
            input("按回车退出...")
            return
        print("✓ 旧进程已终止，正在重启...")

    print(f"启动 Web 服务于 {URL}")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py")],
        cwd=ROOT,
    )

    # 等服务就绪
    for _ in range(20):
        time.sleep(0.5)
        if port_open(PORT):
            break
    else:
        print("⚠ Flask 服务启动超时，请检查日志")
        proc.wait()
        return

    print("✓ 服务已就绪，正在打开浏览器...")
    webbrowser.open(URL)
    print()
    print("─" * 60)
    print("  系统正在运行。")
    print("  本机访问：" + URL)
    lan_ip = get_lan_ip()
    if lan_ip:
        print(f"  局域网他人访问：http://{lan_ip}:{PORT}")
        print(f"  （把上面的地址告诉同事，他们用浏览器打开即可）")
    print("  关闭本窗口将停止服务。")
    print("─" * 60)

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
