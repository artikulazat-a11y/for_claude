"""Точка входа для сборки ACTestClient.exe (PyInstaller)."""

import sys

from ac_test_client import main

if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    code = main()
    # При запуске двойным щелчком окно не должно закрыться до того, как виден итог.
    if sys.stdin and sys.stdin.isatty():
        input("Нажмите Enter для выхода...")
    sys.exit(code)
