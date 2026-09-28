"""Запуск: python -m ac_emulator [--port 502] [--time-scale 1] ..."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .model import ACModel
from .server import ACServer


def main() -> None:
    ap = argparse.ArgumentParser(prog="ac_emulator", description="Modbus TCP эмулятор кондиционера")
    ap.add_argument("--host", default="0.0.0.0", help="адрес для прослушивания (по умолчанию все интерфейсы)")
    ap.add_argument("--port", type=int, default=502, help="TCP-порт Modbus (по умолчанию 502)")
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="ускорение модельного времени (например 60: сутки за 24 мин)")
    ap.add_argument("--tick", type=float, default=0.1, help="шаг расчёта модели, с")
    ap.add_argument("--no-faults", action="store_true", help="отключить случайные аварии")
    ap.add_argument("--seed", type=int, default=None, help="зерно генератора случайных чисел (повторяемость)")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    model = ACModel(time_scale=args.time_scale, seed=args.seed, faults_enabled=not args.no_faults)
    srv = ACServer(model, host=args.host, port=args.port, tick=args.tick)
    try:
        asyncio.run(srv.run())
    except KeyboardInterrupt:
        print("Остановлено.")
    except OSError as e:
        logging.error("Не удалось открыть порт %d: %s. Порт занят или нужны права администратора "
                      "(в Linux порты ниже 1024) — укажите другой: --port 5020", args.port, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
