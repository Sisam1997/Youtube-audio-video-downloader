#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "Updating yt-dlp and installing app requirements..."
python3 -m pip install --upgrade pip
python3 -m pip install --upgrade yt-dlp flask flask-cors imageio-ffmpeg
echo "Open http://127.0.0.1:5000 in your browser."
python3 app.py
