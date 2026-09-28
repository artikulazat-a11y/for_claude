"""Карта регистров Modbus кондиционера (адреса с нуля, как в запросе Modbus).

Температуры передаются как знаковое 16-битное целое в десятых долях °C
(235 = 23.5 °C, 65486 = -5.0 °C). Карта описана в ac_emulator/README.md.
"""

from __future__ import annotations

from .model import ACModel


class IllegalAddress(Exception):
    """Адрес не существует или регистр только для чтения -> исключение Modbus 02."""


class IllegalValue(Exception):
    """Значение вне допустимого диапазона -> исключение Modbus 03."""


def _t(value: float) -> int:
    return round(value * 10) & 0xFFFF


# ---------------------------------------------------------------- Coils (FC 1, 5, 15)
COILS = {
    0: "Включение (1 — вкл, 0 — выкл)",
    1: "Сброс аварии (запись 1; читается 0)",
}


def read_coil(m: ACModel, addr: int) -> bool:
    if addr == 0:
        return m.power
    if addr == 1:
        return False
    raise IllegalAddress


def write_coil(m: ACModel, addr: int, value: bool) -> None:
    if addr == 0:
        m.set_power(value)
    elif addr == 1:
        if value:
            m.reset_fault()
    else:
        raise IllegalAddress


# ---------------------------------------------------------------- Discrete inputs (FC 2)
DISCRETE_INPUTS = {
    0: "Работает (включён и нет аварии)",
    1: "Авария",
    2: "Компрессор работает",
    3: "Вентилятор вращается",
    4: "Охлаждение",
    5: "Обогрев",
}


def read_discrete(m: ACModel, addr: int) -> bool:
    values = (m.running, bool(m.fault_code), m.compressor_percent > 0, m.fan_percent > 0,
              m.compressor_percent > 0 and m.active == 1, m.compressor_percent > 0 and m.active == -1)
    if 0 <= addr < len(values):
        return values[addr]
    raise IllegalAddress


# ---------------------------------------------------------------- Holding registers (FC 3, 6, 16)
HOLDING = {
    0: "Включение: 0 — выкл, 1 — вкл",
    1: "Уставка температуры, 0.1 °C (160..300)",
    2: "Режим: 0 — авто, 1 — охлаждение, 2 — обогрев, 3 — вентиляция",
    3: "Скорость вентилятора: 0 — авто, 1 — низкая, 2 — средняя, 3 — высокая",
    4: "Сброс аварии: запись 1 (читается 0)",
    5: "Тестовая авария: запись кода 1..5 (читается 0)",
}


def read_holding(m: ACModel, addr: int) -> int:
    values = (int(m.power), round(m.setpoint * 10), m.mode, m.fan_mode, 0, 0)
    if 0 <= addr < len(values):
        return values[addr]
    raise IllegalAddress


def write_holding(m: ACModel, addr: int, value: int) -> None:
    try:
        if addr == 0:
            if value not in (0, 1):
                raise ValueError
            m.set_power(bool(value))
        elif addr == 1:
            m.set_setpoint(value / 10)
        elif addr == 2:
            m.set_mode(value)
        elif addr == 3:
            m.set_fan_mode(value)
        elif addr == 4:
            if value not in (0, 1):
                raise ValueError
            if value:
                m.reset_fault()
        elif addr == 5:
            if value:
                m.trigger_fault(value)
        else:
            raise IllegalAddress
    except ValueError as e:
        raise IllegalValue(str(e)) from e


# ---------------------------------------------------------------- Input registers (FC 4)
INPUT = {
    0: "Температура в помещении, 0.1 °C",
    1: "Температура на улице, 0.1 °C",
    2: "Температура приточного воздуха, 0.1 °C",
    3: "Скорость вентилятора, %",
    4: "Обороты вентилятора, об/мин",
    5: "Производительность компрессора, %",
    6: "Состояние: 0 — выкл, 1 — ожидание (уставка достигнута), 2 — охлаждение, 3 — обогрев, 4 — вентиляция, 5 — авария",
    7: "Код аварии (0 — нет)",
    8: "Слово состояния, биты: 0 — вкл, 1 — работает, 2 — компрессор, 3 — вентилятор, 4 — охлаждение, 5 — обогрев, 6 — авария",
    9: "Потребляемая мощность, Вт",
    10: "Счётчик аварий с момента запуска",
}


def read_input(m: ACModel, addr: int) -> int:
    if addr == 8:
        bits = (m.power, m.running, m.compressor_percent > 0, m.fan_percent > 0,
                m.compressor_percent > 0 and m.active == 1, m.compressor_percent > 0 and m.active == -1,
                bool(m.fault_code))
        return sum(1 << i for i, b in enumerate(bits) if b)
    values = (
        _t(m.room_temp_measured),
        _t(m.outdoor_temp),
        _t(m.supply_temp),
        round(m.fan_percent),
        round(m.fan_rpm),
        round(m.compressor_percent),
        m.state,
        m.fault_code,
        None,
        round(m.power_watts),
        min(m.fault_count, 0xFFFF),
    )
    if 0 <= addr < len(values):
        return values[addr]
    raise IllegalAddress
