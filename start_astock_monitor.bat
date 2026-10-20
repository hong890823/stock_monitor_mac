@echo off
chcp 65001 >nul
title A股实时监控系统

echo ╔═══════════════════════════════════════════════════════════╗
echo ║           A股实时数据监控系统                             ║
echo ╚═══════════════════════════════════════════════════════════╝
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 错误: 未找到Python，请先安装Python
    pause
    exit /b 1
)

echo 🚀 启动A股监控...
echo.
python "%~dp0astock_monitor.py"
pause
