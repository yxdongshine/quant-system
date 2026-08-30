@echo off
rem 每日收盘扫描（由计划任务 QuantDailyScan 15:20 调用）
chcp 65001 >nul
cd /d %~dp0
if not exist logs mkdir logs
echo. >> logs\scan.log
echo ===== %date% %time% ===== >> logs\scan.log
"D:\tool\Python\path\python.exe" daily_scan.py >> logs\scan.log 2>&1
