"""Точка входа для сборки ACEmulator.exe (PyInstaller)."""

import sys

from ac_emulator.__main__ import main

if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # При запуске двойным щелчком окно закрылось бы раньше, чем видна ошибка.
        if e.code and sys.stdin and sys.stdin.isatty():
            input("Нажмите Enter для выхода...")
        raise
