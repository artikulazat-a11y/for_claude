@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --name SubstationEmulator --collect-submodules asyncua --paths . packaging/entry.py
echo.
echo Готово: dist\SubstationEmulator.exe
pause
