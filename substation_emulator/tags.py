"""Перечень тегов эмулятора ПС 110/10 кВ.

Путь тега (path) одновременно является строковым NodeId в OPC UA:
``ns=<idx>;s=<path>``, например ``ns=2;s=T1.AVR.Uset``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

# Тип доступа
READ = "R"  # измерение / состояние, только чтение
WRITE = "W"  # уставка / режим, чтение и запись
CMD = "CMD"  # команда: клиент пишет True, эмулятор исполняет и сбрасывает в False

# Положение коммутационного аппарата (как Dbpos в МЭК 61850)
INTERMEDIATE, OPEN, CLOSED = 0, 1, 2
STATE_TEXT = "0-промежуточное, 1-отключен, 2-включен"


@dataclass(frozen=True)
class Tag:
    path: str
    type: str  # Double | Boolean | Int32 | UInt32 | String
    access: str
    desc: str
    unit: str = ""
    lo: float | None = None  # допустимый диапазон уставки
    hi: float | None = None
    default: object = None
    persist: bool = True  # сохранять уставку между перезапусками

    @property
    def writable(self) -> bool:
        return self.access in (WRITE, CMD)


DEFAULTS_BY_TYPE = {"Double": 0.0, "Boolean": False, "Int32": 0, "UInt32": 0, "String": ""}

# Присоединения. Имена — для описаний и журнала событий.
LINES = {1: "ВЛ-110 кВ №1", 2: "ВЛ-110 кВ №2"}
TRANSFORMERS = {1: "Т1 (ТДН-16000/110/10)", 2: "Т2 (ТДН-16000/110/10)"}
SECTIONS = {1: "1 СШ 10 кВ", 2: "2 СШ 10 кВ"}

# Фидеры 10 кВ: tag -> (название, секция, профиль нагрузки, Pmax МВт, cos φ, уставка МТЗ А, выдержка МТЗ с)
FEEDERS = {
    "F1": ("Ф-101 ЖК «Северный»", 1, "residential", 3.4, 0.93, 400.0, 0.5),
    "F2": ("Ф-102 Завод ЖБИ", 1, "industrial", 4.6, 0.86, 600.0, 0.7),
    "F3": ("Ф-201 Мкр «Южный»", 2, "residential", 3.0, 0.94, 400.0, 0.5),
    "F4": ("Ф-202 ТЦ «Парк»", 2, "commercial", 2.6, 0.90, 350.0, 0.5),
}


def _switch(prefix: str, title: str) -> list[Tag]:
    return [
        Tag(f"{prefix}.State", "Int32", READ, f"{title}: положение ({STATE_TEXT})"),
        Tag(f"{prefix}.CmdClose", "Boolean", CMD, f"{title}: команда «Включить»"),
        Tag(f"{prefix}.CmdOpen", "Boolean", CMD, f"{title}: команда «Отключить»"),
    ]


def build_tags() -> list[Tag]:
    t: list[Tag] = [
        Tag("Station.Heartbeat", "UInt32", READ, "Счётчик жизни, +1 каждую секунду"),
        Tag("Station.SimTime", "String", READ, "Модельное время (с учётом ускорения)"),
        Tag("Station.AmbientTemp", "Double", READ, "Температура наружного воздуха", "°C"),
        Tag("Station.Frequency", "Double", READ, "Частота сети", "Гц"),
        Tag("Station.BatteryVoltage", "Double", READ, "Напряжение ЩПТ 220 В", "В"),
        Tag("Station.RemoteMode", "Boolean", WRITE,
            "Режим управления: 1-дистанционный (команды SCADA разрешены), 0-местный", default=True),
        Tag("Station.GeneralAlarm", "Boolean", READ, "Обобщённая аварийная сигнализация"),
        Tag("Station.GeneralWarning", "Boolean", READ, "Обобщённая предупредительная сигнализация"),
        Tag("Station.AlarmReset", "Boolean", CMD, "Квитирование: сброс указателей срабатывания"),
        Tag("Station.LastEvent", "String", READ, "Последнее событие журнала"),
    ]

    for n, title in LINES.items():
        p = f"VL{n}"
        t += [
            Tag(f"{p}.U", "Double", READ, f"{title}: линейное напряжение", "кВ"),
            Tag(f"{p}.SimLoss", "Boolean", WRITE,
                f"{title}: имитация исчезновения напряжения на линии", default=False, persist=False),
        ]
        t += _switch(f"{p}.DS", f"{title}, линейный разъединитель")
        t += _switch(f"{p}.CB", f"{title}, выключатель 110 кВ")

    for n, title in TRANSFORMERS.items():
        p = f"T{n}"
        t += [
            Tag(f"{p}.P", "Double", READ, f"{title}: активная мощность (сторона ВН)", "МВт"),
            Tag(f"{p}.Q", "Double", READ, f"{title}: реактивная мощность (сторона ВН)", "Мвар"),
            Tag(f"{p}.I_HV", "Double", READ, f"{title}: ток стороны ВН", "А"),
            Tag(f"{p}.U_LV", "Double", READ, f"{title}: напряжение на выводах НН", "кВ"),
            Tag(f"{p}.Load", "Double", READ, f"{title}: загрузка от номинальной мощности", "%"),
            Tag(f"{p}.OilTemp", "Double", READ, f"{title}: температура верхних слоёв масла", "°C"),
            Tag(f"{p}.WindingTemp", "Double", READ, f"{title}: температура наиболее нагретой точки обмотки", "°C"),
            Tag(f"{p}.Tap.Position", "Int32", READ, f"{title}: положение РПН (1…19, 10 — номинал)"),
            Tag(f"{p}.Tap.InProgress", "Boolean", READ, f"{title}: РПН в процессе переключения"),
            Tag(f"{p}.Tap.CmdRaise", "Boolean", CMD, f"{title}: РПН «Прибавить» (только в ручном режиме АРНТ)"),
            Tag(f"{p}.Tap.CmdLower", "Boolean", CMD, f"{title}: РПН «Убавить» (только в ручном режиме АРНТ)"),
            Tag(f"{p}.AVR.Auto", "Boolean", WRITE, f"{title}: АРНТ — 1-автоматический режим, 0-ручной", default=True),
            Tag(f"{p}.AVR.Uset", "Double", WRITE, f"{title}: АРНТ — уставка напряжения", "кВ", 9.5, 11.5, 10.5),
            Tag(f"{p}.AVR.Deadband", "Double", WRITE, f"{title}: АРНТ — зона нечувствительности", "%", 0.5, 5.0, 1.2),
            Tag(f"{p}.AVR.Delay", "Double", WRITE, f"{title}: АРНТ — выдержка времени", "с", 5.0, 300.0, 30.0),
            Tag(f"{p}.Cooling.FansOn", "Boolean", READ, f"{title}: вентиляторы обдува (система охлаждения Д) включены"),
            Tag(f"{p}.Cooling.FanOnTemp", "Double", WRITE,
                f"{title}: уставка включения обдува по температуре масла", "°C", 30.0, 80.0, 55.0),
            Tag(f"{p}.Alarm.OilTemp", "Boolean", READ, f"{title}: сигнал «Повышение температуры масла»"),
            Tag(f"{p}.Alarm.OilTempSet", "Double", WRITE,
                f"{title}: уставка сигнала повышения температуры масла", "°C", 60.0, 105.0, 85.0),
            Tag(f"{p}.Alarm.Overload", "Boolean", READ, f"{title}: сигнал «Перегрузка» (>105% более 10 с)"),
        ]

    for n, title in SECTIONS.items():
        p = f"Sec{n}"
        t += [Tag(f"{p}.U", "Double", READ, f"{title}: линейное напряжение", "кВ")]
        t += _switch(f"{p}.InCB", f"{title}, вводной выключатель от Т{n}")
        t += [Tag(f"{p}.InCB.I", "Double", READ, f"{title}, вводной выключатель: ток", "А")]

    t += _switch("SecCB", "Секционный выключатель 10 кВ (СВ)")
    t += [
        Tag("SecCB.I", "Double", READ, "Секционный выключатель 10 кВ: ток", "А"),
        Tag("ABR.Enabled", "Boolean", WRITE, "АВР 10 кВ: 1-введён, 0-выведен", default=True),
        Tag("ABR.Delay", "Double", WRITE, "АВР 10 кВ: выдержка времени", "с", 0.5, 30.0, 2.0),
        Tag("ABR.Operated", "Boolean", READ, "АВР 10 кВ: сработал (сброс квитированием)"),
    ]

    for p, (title, _sec, _prof, _pmax, _cos, iset, tset) in FEEDERS.items():
        t += [
            Tag(f"{p}.P", "Double", READ, f"{title}: активная мощность", "МВт"),
            Tag(f"{p}.Q", "Double", READ, f"{title}: реактивная мощность", "Мвар"),
            Tag(f"{p}.I", "Double", READ, f"{title}: ток", "А"),
        ]
        t += _switch(f"{p}.CB", f"{title}, выключатель")
        t += [
            Tag(f"{p}.Prot.Iset", "Double", WRITE, f"{title}: МТЗ — уставка по току", "А", 50.0, 2000.0, iset),
            Tag(f"{p}.Prot.Tset", "Double", WRITE, f"{title}: МТЗ — выдержка времени", "с", 0.1, 5.0, tset),
            Tag(f"{p}.Prot.Trip", "Boolean", READ, f"{title}: срабатывание МТЗ (сброс квитированием)"),
            Tag(f"{p}.SimFault", "Boolean", WRITE,
                f"{title}: имитация КЗ на линии (устойчивое, до снятия)", default=False, persist=False),
        ]

    return [
        tag if tag.default is not None else _with_default(tag)
        for tag in t
    ]


def _with_default(tag: Tag) -> Tag:
    from dataclasses import replace

    return replace(tag, default=DEFAULTS_BY_TYPE[tag.type])


TAGS: list[Tag] = build_tags()
TAGS_BY_PATH: dict[str, Tag] = {t.path: t for t in TAGS}


def tags_csv(namespace_index: int = 2) -> str:
    """Таблица тегов в CSV (разделитель «;») — для импорта в SCADA."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["NodeId", "Path", "Type", "Access", "Unit", "Min", "Max", "Default", "Description"])
    for t in TAGS:
        w.writerow([
            f"ns={namespace_index};s={t.path}", t.path, t.type, t.access, t.unit,
            "" if t.lo is None else t.lo, "" if t.hi is None else t.hi,
            t.default if t.writable else "", t.desc,
        ])
    return buf.getvalue()
