@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение Python...
  %PY% -m venv .venv || goto :err
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || goto :err
".venv\Scripts\python.exe" pvu_emulator.py %*
goto :eof

:err
echo.
echo Не удалось подготовить окружение. Нужен Python 3.10 или новее: https://www.python.org/downloads/
pause
