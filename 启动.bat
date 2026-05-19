@echo off
chcp 65001 >nul
title 华阳精机简历筛选系统
cd /d "%~dp0"
python launcher.py
pause
