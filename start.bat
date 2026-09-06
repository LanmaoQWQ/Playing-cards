@echo off
cd /d "%~dp0"
echo Starting Bluff Cards server...
python server.py --host 0.0.0.0 --port 8080
pause
