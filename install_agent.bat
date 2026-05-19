@echo off
chcp 65001 >nul
echo.
echo  =========================================
echo   51job 抓取代理 · 安装为开机自启动服务
echo  =========================================
echo.

set "AGENT=%~dp0node_agent.py"
set "PYTHON=python"

%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause & exit /b 1
)

schtasks /create /tn "51job_Scrape_Agent" ^
    /tr "\"%PYTHON%\" \"%AGENT%\"" ^
    /sc onlogon /rl highest /f >nul 2>&1

if errorlevel 1 (
    schtasks /create /tn "51job_Scrape_Agent" ^
        /tr "\"%PYTHON%\" \"%AGENT%\"" ^
        /sc onlogon /f >nul 2>&1
)

if errorlevel 1 (
    echo [失败] 注册任务失败，请右键以「管理员」身份运行此脚本
    pause & exit /b 1
)

echo [成功] 已注册开机自启任务「51job_Scrape_Agent」
echo.
echo 正在立即启动代理（后台运行）...
start /b %PYTHON% "%AGENT%"
echo.
echo 完成！下次开机代理将自动启动。
echo 如需卸载，请运行：schtasks /delete /tn "51job_Scrape_Agent" /f
echo.
pause
