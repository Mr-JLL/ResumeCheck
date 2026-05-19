@echo off
chcp 65001 >nul
echo.
echo  =========================================
echo   51job 抓取代理 · 立即启动（前台运行）
echo  =========================================
echo.
echo 关闭此窗口将停止代理，建议使用 install_agent.bat 注册为开机自启服务
echo.
python "%~dp0node_agent.py"
pause
