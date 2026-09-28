"""Тестовый клиент Modbus TCP для эмулятора кондиционера.

Прогоняет сценарий: включение, смена уставки, чтение температуры и скорости
вентилятора, имитация аварии и её сброс. Каждый шаг проверяется, в конце —
итог PASS/FAIL и код возврата 0/1, так что скрипт годится и для CI.

    python -m ac_emulator --port 5020 --time-scale 60
    python tools/ac_test_client.py --host 127.0.0.1 --port 5020

Адреса регистров собраны в REGISTERS ниже — если карта эмулятора изменится,
править нужно только её.
"""

from __future__ import annotations

import argparse
import inspect
import sys
import time
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient


@dataclass(frozen=True)
class Reg:
    kind: str  # "coil" | "discrete" | "holding" | "input"
    address: int
    scale: float = 1.0  # физическое значение = raw / scale


REGISTERS = {
    "power": Reg("coil", 0),               # R/W: 1 — включён
    "fault_reset": Reg("coil", 1),         # W: 1 — сбросить аварию
    "fault": Reg("discrete", 1),           # R: 1 — авария активна
    "setpoint": Reg("holding", 1, 10),     # R/W: уставка, °C ×10, 16..30
    "fault_simulate": Reg("holding", 5),   # W: код тестовой аварии 1..5
    "temperature": Reg("input", 0, 10),    # R: температура в помещении, °C ×10
    "fan_speed": Reg("input", 3),          # R: вентилятор, %
    "state": Reg("input", 6),              # R: 0 выкл, 1 ожидание, 2 охл, 3 обогрев, 4 вент, 5 авария
    "fault_code": Reg("input", 7),         # R: код аварии, 0 — нет
}


class AcClient:
    def __init__(self, host: str, port: int, unit: int) -> None:
        self.client = ModbusTcpClient(host, port=port)
        # pymodbus >= 3.10 называет адрес устройства device_id, раньше — slave.
        params = inspect.signature(self.client.read_coils).parameters
        self._unit_kw = {"device_id" if "device_id" in params else "slave": unit}

    def connect(self) -> bool:
        return self.client.connect()

    def close(self) -> None:
        self.client.close()

    def read(self, name: str) -> float:
        reg = REGISTERS[name]
        readers = {
            "coil": self.client.read_coils,
            "discrete": self.client.read_discrete_inputs,
            "holding": self.client.read_holding_registers,
            "input": self.client.read_input_registers,
        }
        rr = readers[reg.kind](reg.address, count=1, **self._unit_kw)
        if rr.isError():
            raise RuntimeError(f"чтение {name}: {rr}")
        if reg.kind in ("coil", "discrete"):
            return int(rr.bits[0])
        raw = rr.registers[0]
        if raw >= 0x8000:  # int16 — температура бывает отрицательной
            raw -= 0x10000
        return raw / reg.scale

    def write(self, name: str, value: float) -> None:
        reg = REGISTERS[name]
        if reg.kind == "coil":
            rr = self.client.write_coil(reg.address, bool(value), **self._unit_kw)
        elif reg.kind == "holding":
            raw = round(value * reg.scale) & 0xFFFF
            rr = self.client.write_register(reg.address, raw, **self._unit_kw)
        else:
            raise ValueError(f"{name} только для чтения")
        if rr.isError():
            raise RuntimeError(f"запись {name}: {rr}")


def wait_for(predicate, timeout: float, poll: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


def snapshot(ac: AcClient) -> str:
    return (
        f"вкл={ac.read('power')} уставка={ac.read('setpoint'):.1f} "
        f"t={ac.read('temperature'):.1f} вентилятор={ac.read('fan_speed'):.0f}% "
        f"состояние={ac.read('state'):.0f} "
        f"авария={ac.read('fault')} код={ac.read('fault_code'):.0f}"
    )


def run(ac: AcClient, setpoint: float, settle: float) -> bool:
    results: list[tuple[str, bool]] = []

    def check(title: str, ok: bool) -> None:
        results.append((title, ok))
        print(f"  [{'OK' if ok else 'FAIL'}] {title}")

    print("Исходное состояние:", snapshot(ac))

    # Авария с прошлого запуска помешает остальным шагам.
    if ac.read("fault"):
        ac.write("fault_reset", 1)

    print("1. Включение")
    ac.write("power", 1)
    check("кондиционер включён", wait_for(lambda: ac.read("power") == 1, 3))

    print(f"2. Уставка {setpoint:.1f} °C")
    ac.write("setpoint", setpoint)
    check("уставка записана", abs(ac.read("setpoint") - setpoint) < 0.05)

    print("3. Температура и скорость вентилятора")
    t0 = ac.read("temperature")
    check("вентилятор крутится", wait_for(lambda: ac.read("fan_speed") > 0, 5))
    print(f"   ждём до {settle:.0f} с, пока температура пойдёт к уставке...")

    def approaching() -> bool:
        t = ac.read("temperature")
        return abs(t - setpoint) < abs(t0 - setpoint) or abs(t - setpoint) <= 0.5

    moved = wait_for(approaching, settle)
    t1 = ac.read("temperature")
    print(f"   t: {t0:.1f} -> {t1:.1f} °C, вентилятор {ac.read('fan_speed'):.0f}%")
    check("температура движется к уставке", moved)

    print("4. Имитация аварии")
    ac.write("fault_simulate", 1)
    check("авария поднялась", wait_for(lambda: ac.read("fault") == 1, 3))
    print("   ", snapshot(ac))
    check("код аварии ненулевой", ac.read("fault_code") != 0)

    print("5. Сброс аварии")
    ac.write("fault_reset", 1)
    check("авария сброшена", wait_for(lambda: ac.read("fault") == 0, 3))
    check("код аварии обнулён", ac.read("fault_code") == 0)

    print("6. Выключение")
    ac.write("power", 0)
    check("кондиционер выключен", wait_for(lambda: ac.read("power") == 0, 3))
    check("вентилятор остановился", wait_for(lambda: ac.read("fan_speed") == 0, settle))

    print("Итоговое состояние:", snapshot(ac))
    failed = [title for title, ok in results if not ok]
    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(results) - len(failed)}/{len(results)}")
    return not failed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--unit", type=int, default=1, help="Modbus unit / device id")
    p.add_argument("--setpoint", type=float, default=20.0, help="уставка для теста, °C")
    p.add_argument("--settle", type=float, default=120.0,
                   help="таймаут, с, на инерционные шаги: охлаждение и останов "
                        "вентилятора (с --time-scale 60 хватает нескольких секунд)")
    args = p.parse_args()

    ac = AcClient(args.host, args.port, args.unit)
    if not ac.connect():
        print(f"Не удалось подключиться к {args.host}:{args.port}", file=sys.stderr)
        return 2
    try:
        return 0 if run(ac, args.setpoint, args.settle) else 1
    finally:
        ac.close()


if __name__ == "__main__":
    sys.exit(main())
