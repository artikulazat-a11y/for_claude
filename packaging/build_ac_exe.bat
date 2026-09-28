@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
python -m pip install pyinstaller
pyinstaller --noconfirm --onefile --name AirConditionerEmulator --paths . packaging/ac_entry.py
echo.
echo Готово: dist\AirConditionerEmulator.exe
pause
