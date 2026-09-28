"""Запуск: python -m ac_emulator [--base-port 601] [--count 50] ..."""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import sys
from pathlib import Path

from .model import Fleet
from .registers import REGISTERS, registers_csv
from .server import ACServer


def _save_on_console_close(srv: ACServer) -> None:
    """Windows: при закрытии окна консоли (крестиком) успеть сохранить состояние."""
    import ctypes

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def handler(event: int) -> int:
        if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            srv.save_state(force=True)
        return 0  # дальше — стандартная обработка (Ctrl+C -> KeyboardInterrupt, закрытие -> выход)

    srv.console_handler = handler_type(handler)  # держим ссылку, иначе колбэк удалит сборщик мусора
    ctypes.windll.kernel32.SetConsoleCtrlHandler(srv.console_handler, True)


def main() -> None:
    ap = argparse.ArgumentParser(prog="ac_emulator", description="Modbus TCP эмулятор кондиционеров")
    ap.add_argument("--host", default="0.0.0.0", help="адрес для прослушивания (по умолчанию все интерфейсы)")
    ap.add_argument("--base-port", type=int, default=601, help="порт кондиционера №1 (по умолчанию 601)")
    ap.add_argument("--count", type=int, default=50, help="число кондиционеров (по умолчанию 50: порты 601…650)")
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="ускорение модельного времени: нагрев/охлаждение помещений, погода, счётчики")
    ap.add_argument("--tick", type=float, default=0.2, help="шаг расчёта модели, с")
    ap.add_argument("--alarm-period", type=float, default=72.0, metavar="HOURS",
                    help="случайные аварии: наибольший интервал между авариями одного кондиционера, ч "
                         "(наименьший — 24 ч; по умолчанию 72)")
    ap.add_argument("--no-random-alarms", action="store_true", help="отключить случайные аварии")
    ap.add_argument("--state", default=None,
                    help="файл состояния (по умолчанию ac_state.json: рядом с exe или в текущей папке)")
    ap.add_argument("--no-persist", action="store_true", help="не сохранять состояние между запусками")
    ap.add_argument("--seed", type=int, default=None, help="зерно генератора случайных чисел (повторяемость)")
    ap.add_argument("--export-registers", metavar="FILE", help="выгрузить карту регистров в CSV и выйти")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    args = ap.parse_args()

    if args.export_registers:
        with open(args.export_registers, "w", encoding="utf-8-sig", newline="") as f:
            f.write(registers_csv())
        print(f"Выгружено регистров: {len(REGISTERS)} -> {args.export_registers}")
        return
    last_port = args.base_port + args.count - 1
    if args.count < 1 or args.base_port < 1 or last_port > 65535:
        ap.error("порты должны быть в диапазоне 1…65535")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if sys.platform == "win32":
        # Консоль Windows: вывод кириллицы без ошибок кодировки
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    state = args.state
    if state is None:
        # exe хранит состояние рядом с собой, а не в текущей папке ярлыка
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
        state = str(base / "ac_state.json")

    fleet = Fleet(count=args.count, time_scale=args.time_scale, seed=args.seed,
                  random_alarms=not args.no_random_alarms, alarm_period_hours=args.alarm_period)
    srv = ACServer(fleet, host=args.host, base_port=args.base_port, tick=args.tick,
                   state_file=None if args.no_persist else state)
    if sys.platform == "win32" and srv.state_file:
        _save_on_console_close(srv)
    try:
        asyncio.run(srv.run())
    except KeyboardInterrupt:
        srv.save_state(force=True)
        print("Остановлено, состояние кондиционеров сохранено.")
    except Exception as e:
        if isinstance(e, OSError):
            hint = "порты заняты другой программой (возможно, эмулятор уже запущен)"
            if e.errno == errno.EACCES and sys.platform != "win32":
                hint = "порты ниже 1024 в Linux открываются только от root (sudo) или с правом CAP_NET_BIND_SERVICE"
            logging.error("Не удалось открыть порты %d…%d: %s — %s. Другие порты: --base-port 5601",
                          args.base_port, last_port, e, hint)
        else:
            logging.exception("Эмулятор остановлен из-за ошибки")
        if getattr(sys, "frozen", False):
            # exe запущен двойным щелчком — не закрывать окно, чтобы было видно сообщение
            input("Нажмите Enter, чтобы закрыть окно...")
        sys.exit(1)


if __name__ == "__main__":
    main()
