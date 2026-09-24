"""Запуск: python -m substation_emulator [--port 4840] [--time-scale 1] ..."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .model import Model
from .server import SubstationServer, quiet_asyncua_logs
from .tags import TAGS, tags_csv


def main() -> None:
    ap = argparse.ArgumentParser(prog="substation_emulator", description="OPC UA эмулятор ПС 110/10 кВ")
    ap.add_argument("--host", default="0.0.0.0", help="адрес для прослушивания (по умолчанию все интерфейсы)")
    ap.add_argument("--port", type=int, default=4840, help="TCP-порт OPC UA (по умолчанию 4840)")
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="ускорение модельного времени для суточного графика и нагрева (например 60: сутки за 24 мин)")
    ap.add_argument("--tick", type=float, default=0.1, help="шаг расчёта модели, с")
    ap.add_argument("--setpoints", default=None,
                    help="файл для сохранения уставок (по умолчанию setpoints.json: рядом с exe или в текущей папке)")
    ap.add_argument("--no-persist", action="store_true", help="не сохранять уставки между запусками")
    ap.add_argument("--seed", type=int, default=None, help="зерно генератора случайных чисел (повторяемость)")
    ap.add_argument("--export-tags", metavar="FILE", help="выгрузить таблицу тегов в CSV и выйти")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный лог (в т.ч. asyncua)")
    args = ap.parse_args()

    if args.export_tags:
        with open(args.export_tags, "w", encoding="utf-8-sig", newline="") as f:
            f.write(tags_csv())
        print(f"Выгружено тегов: {len(TAGS)} -> {args.export_tags}")
        return

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if not args.verbose:
        logging.getLogger("asyncua").setLevel(logging.ERROR)
        # Ошибку запуска (занятый порт) сообщаем сами, без трассировки asyncua
        logging.getLogger("asyncua.server.server").setLevel(logging.CRITICAL)
        quiet_asyncua_logs()
    if sys.platform == "win32":
        # Консоль Windows: вывод кириллицы без ошибок кодировки
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    setpoints = args.setpoints
    if setpoints is None:
        # exe хранит уставки рядом с собой, а не в текущей папке ярлыка
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
        setpoints = str(base / "setpoints.json")

    model = Model(time_scale=args.time_scale, seed=args.seed)
    srv = SubstationServer(model, host=args.host, port=args.port, tick=args.tick,
                           setpoints_file=None if args.no_persist else setpoints)
    try:
        asyncio.run(srv.run())
    except KeyboardInterrupt:
        print("Остановлено.")
    except Exception as e:
        if isinstance(e, OSError):
            logging.error("Не удалось открыть порт %d: %s. Порт занят другой программой "
                          "(возможно, уже запущен второй эмулятор) — укажите другой: --port 4841", args.port, e)
        else:
            logging.exception("Эмулятор остановлен из-за ошибки")
        if getattr(sys, "frozen", False):
            # exe запущен двойным щелчком — не закрывать окно, чтобы было видно сообщение
            input("Нажмите Enter, чтобы закрыть окно...")
        sys.exit(1)


if __name__ == "__main__":
    main()
