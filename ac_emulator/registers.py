"""Карта регистров Modbus кондиционера.

Одна и та же таблица читается функциями 3 (Holding) и 4 (Input): SCADA может
опрашивать измерения любой из них. Запись — функциями 6 и 16, только в регистры
с доступом W и CMD. Адреса в таблице — с нуля (как в запросе Modbus);
в нотации «4xxxx» регистр с адресом 0 — это 40001.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

# Тип доступа
READ = "R"  # измерение / состояние, только чтение
WRITE = "W"  # уставка / режим, чтение и запись, сохраняется между перезапусками
CMD = "CMD"  # команда: эмулятор исполняет её сразу, при чтении регистр всегда 0

MODES = {0: "авто", 1: "охлаждение", 2: "нагрев", 3: "вентиляция"}
AUTO, COOL, HEAT, FAN_ONLY = 0, 1, 2, 3
FAN_MODES = {0: "авто", 1: "низкая", 2: "средняя", 3: "высокая"}
STATES = {0: "выключен", 1: "охлаждение", 2: "нагрев", 3: "вентиляция",
          4: "ожидание (уставка достигнута или задержка пуска компрессора)", 5: "авария"}
ST_OFF, ST_COOL, ST_HEAT, ST_FAN, ST_IDLE, ST_ALARM = range(6)

# Биты регистра Status
STATUS_BITS = {
    0: "питание включено (команда)",
    1: "кондиционер работает (не выключен и нет аварии)",
    2: "компрессор работает",
    3: "авария",
    4: "уставка достигнута (±1 °C)",
    5: "задержка пуска компрессора (защита от частых пусков)",
    6: "действующий режим — нагрев",
}

# Коды аварий: код -> (обозначение, описание, бывает только при работающем кондиционере)
ALARMS = {
    1: ("E1", "Высокое давление в контуре хладагента", True),
    2: ("E2", "Низкое давление, утечка хладагента", True),
    3: ("E3", "Перегрев компрессора", True),
    4: ("E4", "Неисправность вентилятора внутреннего блока", True),
    5: ("E5", "Неисправность датчика температуры воздуха", False),
    6: ("E6", "Нет связи с наружным блоком", False),
    7: ("E7", "Переполнение дренажного поддона", True),
    8: ("E8", "Напряжение питания вне допуска", False),
}


def alarm_text(code: int) -> str:
    short, text, _ = ALARMS[code]
    return f"{short} «{text}»"


def _enum(d: dict[int, str]) -> str:
    return ", ".join(f"{k}-{v}" for k, v in d.items())


@dataclass(frozen=True)
class Register:
    address: int  # с нуля
    name: str
    type: str  # uint16 | int16 | uint32 (два регистра, старшее слово первым)
    access: str
    desc: str
    unit: str = ""
    scale: float = 1.0  # значение = сырое * scale
    lo: float | None = None  # допустимый диапазон записи, в единицах значения
    hi: float | None = None
    default: object = None

    @property
    def size(self) -> int:
        return 2 if self.type == "uint32" else 1

    @property
    def writable(self) -> bool:
        return self.access in (WRITE, CMD)

    def encode(self, value: float) -> list[int]:
        raw = round(value / self.scale)
        if self.type == "uint32":
            raw = min(max(raw, 0), 0xFFFFFFFF)
            return [raw >> 16, raw & 0xFFFF]
        if self.type == "int16":
            return [min(max(raw, -0x8000), 0x7FFF) & 0xFFFF]
        return [min(max(raw, 0), 0xFFFF)]

    def decode(self, raw: int) -> float | int:
        if self.type == "int16" and raw >= 0x8000:
            raw -= 0x10000
        return raw * self.scale if self.scale != 1.0 else raw


@dataclass(frozen=True)
class Bit:
    address: int
    name: str  # имя значения модели (у катушек Power и AlarmReset совпадает с регистром)
    access: str
    desc: str


REGISTERS: list[Register] = [
    # --- Управление (запись)
    Register(0, "Power", "uint16", WRITE, "Питание: 0-выключить, 1-включить", lo=0, hi=1, default=1),
    Register(1, "Mode", "uint16", WRITE, f"Режим работы: {_enum(MODES)}", lo=0, hi=3, default=AUTO),
    Register(2, "Setpoint", "int16", WRITE, "Уставка температуры воздуха в помещении", "°C", 0.1,
             16.0, 30.0, 22.0),
    Register(3, "FanMode", "uint16", WRITE, f"Скорость вентилятора: {_enum(FAN_MODES)}", lo=0, hi=3, default=0),
    Register(4, "AlarmReset", "uint16", CMD, "Сброс аварии: записать 1", lo=0, hi=1, default=0),
    Register(5, "SimAlarm", "uint16", CMD, "Имитация аварии: записать код аварии 1…8", lo=0, hi=8, default=0),
    # --- Состояние и измерения (чтение)
    Register(10, "State", "uint16", READ, f"Состояние: {_enum(STATES)}"),
    Register(11, "Status", "uint16", READ,
             "Слово состояния, биты: " + ", ".join(f"b{b}-{t}" for b, t in STATUS_BITS.items())),
    Register(12, "RoomTemp", "int16", READ, "Температура воздуха в помещении", "°C", 0.1),
    Register(13, "SupplyTemp", "int16", READ, "Температура воздуха на выходе внутреннего блока", "°C", 0.1),
    Register(14, "OutdoorTemp", "int16", READ, "Температура наружного воздуха", "°C", 0.1),
    Register(15, "FanSpeed", "uint16", READ, "Скорость вращения вентилятора внутреннего блока", "об/мин"),
    Register(16, "FanLevel", "uint16", READ, "Текущая ступень вентилятора: 0-стоит, 1-низкая, 2-средняя, 3-высокая"),
    Register(17, "CompressorFreq", "uint16", READ, "Частота компрессора (0 — компрессор стоит)", "Гц"),
    Register(18, "PowerInput", "uint16", READ, "Потребляемая электрическая мощность", "Вт"),
    Register(19, "AlarmCode", "uint16", READ, "Код активной аварии (0 — нет аварии)"),
    Register(20, "LastAlarmCode", "uint16", READ, "Код последней аварии (сохраняется после сброса)"),
    Register(21, "AlarmCount", "uint16", READ, "Счётчик аварий"),
    Register(22, "Energy", "uint32", READ, "Потреблённая электроэнергия (нарастающим итогом)", "кВт·ч", 0.1),
    Register(24, "RunHours", "uint32", READ, "Наработка (время работы вентилятора)", "ч"),
    Register(26, "Heartbeat", "uint16", READ, "Счётчик жизни, +1 каждую секунду"),
    Register(27, "DeviceNumber", "uint16", READ, "Номер кондиционера (1…50)"),
    Register(28, "Capacity", "uint16", READ, "Номинальная холодопроизводительность", "Вт"),
]

COILS: list[Bit] = [
    Bit(0, "Power", WRITE, "Питание: 0-выключить, 1-включить (то же, что регистр Power)"),
    Bit(1, "AlarmReset", CMD, "Сброс аварии: записать 1"),
]

DISCRETE_INPUTS: list[Bit] = [
    Bit(0, "Running", READ, "Кондиционер работает"),
    Bit(1, "Compressor", READ, "Компрессор работает"),
    Bit(2, "Alarm", READ, "Авария"),
    Bit(3, "SetpointReached", READ, "Уставка достигнута (±1 °C)"),
]

REGISTERS_BY_NAME: dict[str, Register] = {r.name: r for r in REGISTERS}
# Адрес регистра -> (описание, индекс слова внутри значения); пустые адреса читаются как 0
REGISTER_AT: dict[int, tuple[Register, int]] = {
    r.address + i: (r, i) for r in REGISTERS for i in range(r.size)
}
REGISTER_COUNT = max(REGISTER_AT) + 1
SETTINGS = [r.name for r in REGISTERS if r.access == WRITE]


def encode_registers(values: dict[str, float]) -> list[int]:
    """Образ всей таблицы регистров из значений модели."""
    image = [0] * REGISTER_COUNT
    for r in REGISTERS:
        image[r.address:r.address + r.size] = r.encode(values[r.name])
    return image


def registers_csv() -> str:
    """Карта регистров в CSV (разделитель «;») — для импорта в SCADA."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["Table", "Address", "Modbus", "Name", "Type", "Access", "Scale", "Unit",
                "Min", "Max", "Default", "Description"])
    for r in REGISTERS:
        w.writerow(["Holding/Input", r.address, 40001 + r.address, r.name, r.type, r.access,
                    r.scale if r.scale != 1.0 else "", r.unit,
                    "" if r.lo is None else r.lo, "" if r.hi is None else r.hi,
                    "" if r.default is None else r.default, r.desc])
    for table, base, bits in (("Coil", 1, COILS), ("DiscreteInput", 10001, DISCRETE_INPUTS)):
        for b in bits:
            w.writerow([table, b.address, base + b.address, b.name, "bool", b.access, "", "",
                        "", "", "", b.desc])
    return buf.getvalue()
