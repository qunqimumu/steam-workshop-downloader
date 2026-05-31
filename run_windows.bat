@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import customtkinter" 2>nul
if errorlevel 1 (
  echo [INFO] Installing customtkinter...
  python -m pip install -r requirements.txt
)
python steam_workshop_modern_customtk_scrollbar_in_tree.py
pause
