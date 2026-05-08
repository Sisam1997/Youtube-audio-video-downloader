@echo off
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install --upgrade -r requirements.txt
python -m pip install --upgrade yt-dlp
python app.py
pause
