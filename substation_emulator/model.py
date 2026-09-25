"""Физическая модель ПС 110/10 кВ (не зависит от OPC UA).

Схема: две ВЛ-110 кВ -> линейный разъединитель -> выключатель 110 кВ ->
трансформатор ТДН-16000/110/10 с РПН -> вводной выключатель 10 кВ ->
секция шин 10 кВ. Секции связаны секционным выключателем (СВ) с АВР.
На каждой секции по два фидера с МТЗ. Учёт энергии — 10 счётчиков:
Т1/Т2 на стороне 110 кВ, вводы 10 кВ, фидеры, ТСН собственных нужд.

Модель считается шагами ``step(dt)``; клиентские записи приходят через
``write(path, value)``. Результат — словарь ``values`` (путь тега -> значение).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from .tags import (
    CLOSED, CMD, FEEDERS, INTERMEDIATE, LINES, METERS, OPEN, SECTIONS, TAGS, TAGS_BY_PATH, TRANSFORMERS, WRITE,
)

SQRT3 = math.sqrt(3.0)
U_NOM_10 = 10.5  # номинальное напряжение сети 10 кВ для статических характеристик нагрузки, кВ

SEV_INFO, SEV_WARNING, SEV_ALARM = 200, 500, 800

# Суточные графики нагрузки, о.е. от Pmax, по часам 0..23
PROFILES = {
    "residential": [0.45, 0.40, 0.38, 0.37, 0.38, 0.45, 0.60, 0.78, 0.85, 0.80, 0.74, 0.72,
                    0.72, 0.70, 0.68, 0.70, 0.76, 0.86, 0.95, 1.00, 0.98, 0.90, 0.75, 0.58],
    "industrial": [0.35, 0.33, 0.33, 0.33, 0.34, 0.40, 0.62, 0.90, 0.98, 1.00, 0.98, 0.92,
                   0.80, 0.95, 0.98, 0.96, 0.90, 0.70, 0.55, 0.48, 0.45, 0.42, 0.40, 0.37],
    "commercial": [0.30, 0.28, 0.27, 0.27, 0.28, 0.32, 0.40, 0.55, 0.75, 0.90, 0.95, 0.98,
                   1.00, 0.98, 0.97, 0.96, 0.95, 0.93, 0.90, 0.85, 0.75, 0.60, 0.45, 0.35],
}
WEEKEND_FACTOR = {"residential": 1.05, "industrial": 0.45, "commercial": 1.10}
# Сезонность: максимум в середине января (отопление, освещение), минимум летом
SEASON_AMPLITUDE = {"residential": 0.15, "industrial": 0.04, "commercial": 0.08}

# Среднемесячная температура воздуха (средняя полоса), °C
MONTHLY_MEAN_TEMP = [-7, -6, -1, 6, 13, 17, 19, 17, 11, 5, -1, -5]


def profile_value(name: str, t: datetime) -> float:
    """Значение суточного графика с плавной (косинусной) интерполяцией между часами."""
    p = PROFILES[name]
    h = t.hour + t.minute / 60 + t.second / 3600
    i = int(h) % 24
    w = (1 - math.cos(math.pi * (h - int(h)))) / 2
    v = p[i] * (1 - w) + p[(i + 1) % 24] * w
    if t.weekday() >= 5:
        v *= WEEKEND_FACTOR[name]
    v *= 1 + SEASON_AMPLITUDE[name] * math.cos(2 * math.pi * (t.timetuple().tm_yday - 15) / 365.25)
    return v


def ambient_base(t: datetime) -> float:
    """Температура воздуха без случайной составляющей: сезон + суточный ход (мин. в 5:00, макс. в 15:00)."""
    m = t.month - 1
    frac = (t.day - 1) / 30
    mean = MONTHLY_MEAN_TEMP[m] * (1 - frac) + MONTHLY_MEAN_TEMP[(m + 1) % 12] * frac
    h = t.hour + t.minute / 60
    return mean + 5.0 * math.cos(2 * math.pi * (h - 15) / 24)


class OU:
    """Случайный процесс Орнштейна-Уленбека: «живой» шум со среднеквадратичным sigma и памятью tau, с."""

    def __init__(self, rng: random.Random, sigma: float, tau: float):
        self.rng, self.sigma, self.tau = rng, sigma, tau
        self.x = rng.gauss(0, sigma)

    def step(self, dt: float) -> float:
        a = math.exp(-dt / self.tau)
        self.x = self.x * a + self.sigma * math.sqrt(1 - a * a) * self.rng.gauss(0, 1)
        return self.x


class Switch:
    """Коммутационный аппарат с приводом: команда -> промежуточное положение -> конечное."""

    def __init__(self, prefix: str, title: str, closed: bool, op_time: float, rng: random.Random):
        self.prefix, self.title = prefix, title
        self.closed = closed
        self.op_time = op_time
        self.rng = rng
        self.target: bool | None = None
        self.remaining = 0.0

    @property
    def moving(self) -> bool:
        return self.target is not None

    @property
    def state(self) -> int:
        if self.moving:
            return INTERMEDIATE
        return CLOSED if self.closed else OPEN

    def operate(self, close: bool) -> None:
        self.target = close
        self.remaining = self.op_time * self.rng.uniform(0.85, 1.15)

    def step(self, dt: float) -> bool:
        """Возвращает True, если операция завершилась на этом шаге."""
        if self.target is None:
            return False
        self.remaining -= dt
        if self.remaining > 0:
            return False
        self.closed, self.target = self.target, None
        return True


class Line:
    """Ввод ВЛ-110 кВ: напряжение линии, разъединитель, выключатель."""

    def __init__(self, n: int, title: str, rng: random.Random):
        self.n, self.title = n, title
        self.sim_loss = False
        self.ds = Switch(f"VL{n}.DS", f"{title}, линейный разъединитель", True, 6.0, rng)
        self.cb = Switch(f"VL{n}.CB", f"{title}, выключатель 110 кВ", True, 0.08, rng)
        self.offset = OU(rng, 0.15, 300)
        self.U = 0.0


class Transformer:
    """ТДН-16000/110/10: схема замещения, РПН с АРНТ, тепловая модель по ГОСТ 14209 / МЭК 60076-7."""

    S_NOM = 16.0  # МВА
    U_HV, U_LV = 115.0, 11.0  # кВ
    TAP_MIN, TAP_NOM, TAP_MAX, TAP_STEP = 1, 10, 19, 0.0178  # ±9×1.78%
    TAP_TIME = 5.0  # с, время переключения РПН
    UK, PK, P0, I0 = 0.105, 0.085, 0.018, 0.007  # uk о.е., Pк МВт, Pх МВт, Iх о.е.
    I_NOM_LV = S_NOM / (SQRT3 * U_LV)  # кА

    def __init__(self, n: int, title: str):
        self.n, self.title = n, title
        self.R = self.PK * self.U_LV ** 2 / self.S_NOM ** 2  # Ом, приведено к НН
        self.X = self.UK * self.U_LV ** 2 / self.S_NOM
        self.tap = self.TAP_NOM
        self.tap_dir = 0
        self.tap_remaining = 0.0
        self.tap_limit_reported = False
        # АРНТ
        self.avr_auto = True
        self.uset, self.deadband, self.delay = 10.5, 1.2, 30.0
        self.avr_timer = 0.0
        # Охлаждение и сигнализация
        self.fan_on_temp = 55.0
        self.fans = False
        self.fan_load_timer = 0.0
        self.oil_alarm_set = 85.0
        self.oil_alarm = False
        self.overload = False
        self.overload_timer = 0.0
        self.oil = 20.0
        self.dh = 0.0  # превышение ННТ над маслом
        # Режим
        self.energized = False
        self.U_hv = self.U_lv = 0.0
        self.P_lv = self.Q_lv = 0.0
        self.P = self.Q = self.I_hv = self.I_lv = self.K = 0.0

    def ratio(self, tap: int | None = None) -> float:
        tap = self.tap if tap is None else tap
        # «Прибавить» (номер больше) -> меньше витков ВН -> выше напряжение НН
        return self.U_HV * (1 - (tap - self.TAP_NOM) * self.TAP_STEP) / self.U_LV

    def u_lv_loaded(self, P: float, Q: float, tap: int | None = None) -> float:
        u0 = self.U_hv / self.ratio(tap)
        return u0 - (P * self.R + Q * self.X) / u0

    def best_tap(self, P: float, Q: float) -> int:
        return min(range(self.TAP_MIN, self.TAP_MAX + 1),
                   key=lambda k: abs(self.u_lv_loaded(P, Q, k) - self.uset))

    def _thermal_params(self) -> tuple[float, float, float, float]:
        # (Δθор, n, τ масла с, Hgr): ONAF — с обдувом, ONAN — естественное охлаждение
        return (50.0, 0.9, 150 * 60, 26.0) if self.fans else (68.0, 0.8, 180 * 60, 23.0)

    def _thermal_targets(self, ambient: float) -> tuple[float, float]:
        dtor, n, _tau, hgr = self._thermal_params()
        if not self.energized:
            return ambient, 0.0
        r = self.PK / self.P0
        oil_ss = ambient + dtor * ((1 + r * self.K ** 2) / (1 + r)) ** n
        return oil_ss, hgr * self.K ** 1.3

    def init_thermal(self, ambient: float) -> None:
        self.fans = self.K >= 0.9
        self.oil, self.dh = self._thermal_targets(ambient)
        if self.oil >= self.fan_on_temp:
            self.fans = True
            self.oil, self.dh = self._thermal_targets(ambient)

    def thermal_step(self, dt_sim: float, ambient: float) -> None:
        _dtor, _n, tau_o, _hgr = self._thermal_params()
        oil_ss, dh_ss = self._thermal_targets(ambient)
        self.oil += (oil_ss - self.oil) * (1 - math.exp(-dt_sim / tau_o))
        self.dh += (dh_ss - self.dh) * (1 - math.exp(-dt_sim / (7 * 60)))

    @property
    def winding(self) -> float:
        return self.oil + self.dh


class Feeder:
    """Отходящая линия 10 кВ: нагрузка по суточному графику, выключатель, МТЗ, имитация КЗ."""

    def __init__(self, tag: str, spec: tuple, rng: random.Random):
        title, section, profile, pmax, cosphi, iset, tset = spec
        self.tag, self.title, self.section = tag, title, section
        self.profile, self.pmax, self.cosphi = profile, pmax, cosphi
        self.iset, self.tset = iset, tset
        self.cb = Switch(f"{tag}.CB", f"{title}, выключатель", True, 0.07, rng)
        self.rng = rng
        self.sim_fault = False
        self.fault_z = 1.0  # сопротивление до места КЗ, Ом
        self.trip = False
        self.prot_timer = 0.0
        self.slow = OU(rng, 0.05, 900)
        self.fast = OU(rng, 0.012, 3)
        self.cos_noise = OU(rng, 0.008, 600)
        self.energized = True
        self.on_time = 1e9  # сколько секунд линия под напряжением
        self.off_time = 0.0  # длительность предыдущего перерыва питания
        self.pickup = 0.0  # бросок нагрузки после восстановления питания, о.е.
        self.P = self.Q = self.I = 0.0

    def set_energized(self, on: bool, dt: float) -> None:
        if on and not self.energized:
            # Самозапуск / «холодная» нагрузка: тем больше, чем дольше не было питания
            self.pickup = 0.25 + 0.35 * min(1.0, self.off_time / 1800)
            self.on_time = 0.0
        if on:
            self.on_time += dt
            self.off_time = 0.0
        else:
            self.off_time += dt
        self.energized = on

    def demand(self, t: datetime, u: float, dt: float) -> tuple[float, float]:
        self.slow.step(dt)
        self.fast.step(dt)
        self.cos_noise.step(dt)
        p0 = self.pmax * profile_value(self.profile, t) * (1 + self.slow.x + self.fast.x)
        p0 *= 1 + self.pickup * math.exp(-self.on_time / 30)
        cos = min(0.99, max(0.7, self.cosphi + self.cos_noise.x))
        q0 = p0 * math.tan(math.acos(cos))
        vf = u / U_NOM_10 if u > 0 else 1.0
        return max(0.0, p0 * vf), max(0.0, q0 * vf * vf)


class Meter:
    """Счётчик электроэнергии: накапливает A+ (класс 0,5S) и R+ (класс 1) со своей погрешностью."""

    def __init__(self, key: str, rng: random.Random, daily_mwh: float):
        self.key = key
        # Индивидуальная погрешность в пределах класса точности — из-за неё баланс не сходится «в ноль»
        self.err_a = rng.uniform(-0.003, 0.003)
        self.err_r = rng.uniform(-0.006, 0.006)
        # Показания счётчика, проработавшего 1–4 года
        days = rng.uniform(365, 1500)
        self.a = round(daily_mwh * 1000 * days)  # кВт·ч
        self.r = round(self.a * rng.uniform(0.35, 0.55))  # квар·ч

    def add(self, p_mw: float, q_mvar: float, hours: float) -> None:
        self.a += max(0.0, p_mw) * 1000 * hours * (1 + self.err_a)
        self.r += max(0.0, q_mvar) * 1000 * hours * (1 + self.err_r)


class Model:
    TICK_ANALOG = 0.5  # период обновления аналоговых измерений, с
    X_SYSTEM = 0.06  # сопротивление энергосистемы, приведённое к 10 кВ, Ом

    def __init__(self, time_scale: float = 1.0, seed: int | None = None, start: datetime | None = None):
        self.rng = rng = random.Random(seed)
        self.time_scale = time_scale
        self.clock = start or datetime.now()
        self.values: dict[str, object] = {t.path: t.default for t in TAGS}
        self.events: list[tuple[int, str]] = []
        self.setpoints_dirty = False
        self._cmds: list[str] = []
        self._analog_timer = 0.0
        self._force_analog = True
        self._uptime = 0.0

        self.remote = True
        self.lines = {n: Line(n, title, rng) for n, title in LINES.items()}
        self.trafos = {n: Transformer(n, title) for n, title in TRANSFORMERS.items()}
        self.incb = {n: Switch(f"Sec{n}.InCB", f"{title}, вводной выключатель", True, 0.07, rng)
                     for n, title in SECTIONS.items()}
        self.seccb = Switch("SecCB", "Секционный выключатель 10 кВ", False, 0.07, rng)
        self.feeders = {tag: Feeder(tag, spec, rng) for tag, spec in FEEDERS.items()}
        daily = {key: 110.0 if key[0] in "TI" else 0.8 if key.startswith("Aux")
                 else FEEDERS[key][3] * 0.65 * 24 for key in METERS}
        self.meters = {key: Meter(key, rng, daily[key]) for key in METERS}

        self.abr_enabled, self.abr_delay, self.abr_operated = True, 2.0, False
        self._abr_timer = {1: 0.0, 2: 0.0}
        self._abr_section: int | None = None

        self.switches: dict[str, Switch] = {}
        for ln in self.lines.values():
            self.switches[ln.ds.prefix] = ln.ds
            self.switches[ln.cb.prefix] = ln.cb
        for sw in self.incb.values():
            self.switches[sw.prefix] = sw
        self.switches[self.seccb.prefix] = self.seccb
        for f in self.feeders.values():
            self.switches[f.cb.prefix] = f.cb

        # Куда попадает записанная клиентом уставка: путь -> (объект, атрибут)
        self._setpoints: dict[str, tuple[object, str]] = {
            "Station.RemoteMode": (self, "remote"),
            "ABR.Enabled": (self, "abr_enabled"),
            "ABR.Delay": (self, "abr_delay"),
        }
        for n, ln in self.lines.items():
            self._setpoints[f"VL{n}.SimLoss"] = (ln, "sim_loss")
        for n, tr in self.trafos.items():
            for tag, attr in (("AVR.Auto", "avr_auto"), ("AVR.Uset", "uset"), ("AVR.Deadband", "deadband"),
                              ("AVR.Delay", "delay"), ("Cooling.FanOnTemp", "fan_on_temp"),
                              ("Alarm.OilTempSet", "oil_alarm_set")):
                self._setpoints[f"T{n}.{tag}"] = (tr, attr)
        for tag, f in self.feeders.items():
            for name, attr in (("Prot.Iset", "iset"), ("Prot.Tset", "tset"), ("SimFault", "sim_fault")):
                self._setpoints[f"{tag}.{name}"] = (f, attr)

        # Сетевые шумы
        self.u_slow = OU(rng, 0.8, 1200)
        self.u_fast = OU(rng, 0.12, 4)
        self.f_slow = OU(rng, 0.012, 90)
        self.f_fast = OU(rng, 0.003, 2)
        self.t_noise = OU(rng, 0.6, 1800)
        self.aux_noise = OU(rng, 0.04, 120)
        self.aux_p = {1: 0.0, 2: 0.0}
        self.aux_q = {1: 0.0, 2: 0.0}
        self.frequency = 50.0
        self.ambient = ambient_base(self.clock)
        self.battery = 232.0
        self.aux = True
        self.sec_u = {1: U_NOM_10, 2: U_NOM_10}
        self.sec_src: dict[int, int | None] = {1: 1, 2: 2}
        self.sec_i = {1: 0.0, 2: 0.0}
        self._line_u_prev = {n: True for n in self.lines}
        self._sec_u_prev = {n: True for n in SECTIONS}

        self._init_steady_state()

    # ------------------------------------------------------------------ API

    def check(self, path: str, value: object) -> tuple[object, str | None]:
        """Проверка записи: (значение, приведённое к типу тега; None или имя кода ошибки OPC UA)."""
        tag = TAGS_BY_PATH.get(path)
        if tag is None or not tag.writable:
            return value, "BadNotWritable"
        if isinstance(value, str) or value is None:
            return value, "BadTypeMismatch"
        try:
            value = float(value) if tag.type == "Double" else bool(value)
        except (TypeError, ValueError):
            return value, "BadTypeMismatch"
        if tag.type == "Double" and not math.isfinite(value):
            return value, "BadOutOfRange"
        if tag.lo is not None and not (tag.lo <= value <= tag.hi):
            return value, "BadOutOfRange"
        return value, None

    def write(self, path: str, value: object) -> str | None:
        """Запись от клиента. Возвращает None или имя кода ошибки OPC UA (BadNotWritable, BadOutOfRange…)."""
        value, err = self.check(path, value)
        if err:
            return err
        tag = TAGS_BY_PATH[path]

        if tag.access == CMD:
            if value:
                self._cmds.append(path)
            self.values[path] = value
            return None

        old = self.values[path]
        obj, attr = self._setpoints[path]
        setattr(obj, attr, value)
        self.values[path] = value
        if tag.persist:
            self.setpoints_dirty = True
        if value != old:
            self._on_setpoint_changed(path, value)
        return None

    def state(self) -> dict[str, object]:
        """Сохраняемое состояние: уставки и показания счётчиков (как энергонезависимая память)."""
        data: dict[str, object] = {t.path: self.values[t.path] for t in TAGS if t.access == WRITE and t.persist}
        for key, m in self.meters.items():
            data[f"Meter.{key}.Aplus"] = round(m.a, 3)
            data[f"Meter.{key}.Rplus"] = round(m.r, 3)
        return data

    def load_state(self, data: dict[str, object]) -> None:
        for path, value in data.items():
            tag = TAGS_BY_PATH.get(path)
            if tag is None:
                continue
            if path.startswith("Meter."):
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and math.isfinite(value) and value >= 0:
                    _, key, kind = path.split(".")
                    setattr(self.meters[key], "a" if kind == "Aplus" else "r", float(value))
            elif tag.access == WRITE and tag.persist:
                if self.write(path, value) is not None:
                    self.values[path] = tag.default
        self.events.clear()
        self.values["Station.LastEvent"] = ""
        self.setpoints_dirty = False
        self._init_steady_state()

    def pop_events(self) -> list[tuple[int, str]]:
        ev, self.events = self.events, []
        return ev

    def step(self, dt: float) -> None:
        self._uptime += dt
        self.clock += timedelta(seconds=dt * self.time_scale)
        for path in self._cmds:
            self._command(path)
            self.values[path] = False
        self._cmds.clear()

        for sw in self.switches.values():
            if sw.step(dt):
                self._force_analog = True
                self._event(SEV_INFO, f"{sw.title}: {'включен' if sw.closed else 'отключен'}")

        self._solve(dt)
        self._metering(dt * self.time_scale)
        self._protection(dt)
        self._abr(dt)
        for tr in self.trafos.values():
            self._tap_changer(tr, dt)
            self._cooling(tr, dt)
            tr.thermal_step(dt * self.time_scale, self.ambient)
        self._station(dt)
        self._alarms(dt)

        self._analog_timer += dt
        analog = self._force_analog or self._analog_timer >= self.TICK_ANALOG
        if analog:
            self._analog_timer = 0.0
            self._force_analog = False
        self._publish(analog)

    # ---------------------------------------------------------- внутреннее

    def _event(self, severity: int, text: str) -> None:
        self.events.append((severity, text))
        self.values["Station.LastEvent"] = f"{datetime.now():%H:%M:%S} {text}"

    def _init_steady_state(self) -> None:
        """Стартуем из установившегося режима: РПН в нужном положении, масло прогрето."""
        self._solve(0.1)
        for tr in self.trafos.values():
            if tr.energized:
                tr.tap = tr.best_tap(tr.P_lv, tr.Q_lv)
        self._solve(0.1)
        for tr in self.trafos.values():
            tr.init_thermal(self.ambient)
        self._force_analog = True
        self._publish(True)

    def _on_setpoint_changed(self, path: str, value: object) -> None:
        tag = TAGS_BY_PATH[path]
        if path.endswith(".SimFault"):
            f = self.feeders[path.split(".")[0]]
            if value:
                f.fault_z = self.rng.uniform(0.6, 2.5)
                self._event(SEV_WARNING, f"{f.title}: имитация КЗ на линии")
            else:
                self._event(SEV_INFO, f"{f.title}: имитация КЗ снята")
        elif path.endswith(".SimLoss"):
            ln = self.lines[int(path[2])]
            self._event(SEV_WARNING if value else SEV_INFO,
                        f"{ln.title}: имитация {'исчезновения' if value else 'восстановления'} напряжения")
        elif path == "Station.RemoteMode":
            self._event(SEV_WARNING, f"Режим управления: {'дистанционный' if value else 'местный'}")
        elif path.endswith("AVR.Auto"):
            tr = self.trafos[int(path[1])]
            tr.avr_timer = 0.0
            self._event(SEV_INFO, f"{tr.title}: АРНТ переведён в {'автоматический' if value else 'ручной'} режим")
        else:
            unit = f" {tag.unit}" if tag.unit else ""
            self._event(SEV_INFO, f"Изменена уставка: {tag.desc} = {value}{unit}")

    def _command(self, path: str) -> None:
        prefix, action = path.rsplit(".", 1)
        if path == "Station.AlarmReset":
            for f in self.feeders.values():
                f.trip = False
            self.abr_operated = False
            self._event(SEV_INFO, "Квитирование сигнализации")
            return

        if action in ("CmdRaise", "CmdLower"):
            tr = self.trafos[int(prefix[1])]
            name = "Прибавить" if action == "CmdRaise" else "Убавить"
            reason = None
            if not self.remote:
                reason = "местный режим управления"
            elif tr.avr_auto:
                reason = "АРНТ в автоматическом режиме"
            elif tr.tap_dir:
                reason = "РПН в процессе переключения"
            elif not self.aux:
                reason = "нет питания собственных нужд"
            if reason:
                self._event(SEV_WARNING, f"{tr.title}: команда РПН «{name}» отклонена — {reason}")
            else:
                self._start_tap(tr, 1 if action == "CmdRaise" else -1, "команда оператора")
            return

        sw = self.switches[prefix]
        close = action == "CmdClose"
        name = "Включить" if close else "Отключить"
        reason = "местный режим управления" if not self.remote else self._interlock(sw, close)
        if reason:
            self._event(SEV_WARNING, f"{sw.title}: команда «{name}» отклонена — {reason}")
            return
        self._event(SEV_INFO, f"{sw.title}: команда «{name}»")
        sw.operate(close)
        self._force_analog = True

    def _interlock(self, sw: Switch, close: bool) -> str | None:
        """Оперативные блокировки. Возвращает причину запрета или None."""
        if sw.moving:
            return "аппарат в движении"
        if sw.closed == close:
            return f"аппарат уже {'включен' if close else 'отключен'}"
        for ln in self.lines.values():
            if sw is ln.ds and (ln.cb.closed or ln.cb.moving):
                return "включен выключатель 110 кВ (операции разъединителем под нагрузкой запрещены)"
        if close and sw is self.seccb and self.incb[1].closed and self.incb[2].closed:
            return "включены оба вводных выключателя (параллельная работа Т1 и Т2 запрещена)"
        for n, incb in self.incb.items():
            if close and sw is incb and self.seccb.closed and self.incb[3 - n].closed:
                return "включены СВ и ввод другой секции (параллельная работа Т1 и Т2 запрещена)"
        return None

    def _solve(self, dt: float) -> None:
        """Расчёт установившегося режима на текущем шаге."""
        t = self.clock
        system_load = (profile_value("residential", t) + profile_value("industrial", t)) / 2
        grid = 116.0 - 2.5 * (system_load - 0.6) + self.u_slow.step(dt) + self.u_fast.step(dt)
        for ln in self.lines.values():
            ln.offset.step(dt)
            ln.U = 0.0 if ln.sim_loss else grid + ln.offset.x

        for n, tr in self.trafos.items():
            ln = self.lines[n]
            tr.energized = ln.U > 0 and ln.ds.closed and ln.cb.closed
            tr.U_hv = ln.U if tr.energized else 0.0

        # Топология 10 кВ: от какого трансформатора питается каждая секция
        src: dict[int, int | None] = {}
        for s in (1, 2):
            src[s] = s if self.incb[s].closed and self.trafos[s].energized else None
        for s in (1, 2):
            o = 3 - s
            if src[s] is None and self.seccb.closed and src[o] == o:
                src[s] = o
        self.sec_src = src

        # Нагрузки секций (по напряжению предыдущего шага)
        sec_p = {1: 0.0, 2: 0.0}
        sec_q = {1: 0.0, 2: 0.0}
        faults: dict[int, list[Feeder]] = {1: [], 2: []}
        for f in self.feeders.values():
            s = f.section
            on = src[s] is not None and f.cb.closed
            f.set_energized(on, dt)
            if not on:
                f.P = f.Q = f.I = 0.0
                f.slow.step(dt)
                continue
            if f.sim_fault:
                faults[s].append(f)
                continue
            f.P, f.Q = f.demand(t, self.sec_u[s], dt)
            sec_p[s] += f.P
            sec_q[s] += f.Q
        # Собственные нужды: при потере одной секции вся нагрузка переходит на ТСН другой (АВР 0,4 кВ)
        self.aux_noise.step(dt)
        live = [s for s in (1, 2) if src[s] is not None]
        aux_p, aux_q = self._aux_load()
        for s in (1, 2):
            self.aux_p[s] = aux_p / len(live) if s in live else 0.0
            self.aux_q[s] = aux_q / len(live) if s in live else 0.0
            sec_p[s] += self.aux_p[s]
            sec_q[s] += self.aux_q[s]

        # Режим трансформаторов
        for n, tr in self.trafos.items():
            fed = [s for s in (1, 2) if src[s] == n]
            P = sum(sec_p[s] for s in fed)
            Q = sum(sec_q[s] for s in fed)
            if not tr.energized:
                tr.U_lv = tr.P_lv = tr.Q_lv = tr.I_lv = 0.0
                tr.P = tr.Q = tr.I_hv = tr.K = 0.0
                continue
            u0 = tr.U_hv / tr.ratio()
            flt = [f for s in fed for f in faults[s]]
            if flt:
                # КЗ: ток ограничен сопротивлением системы, трансформатора и линии до места КЗ
                worst = min(flt, key=lambda f: f.fault_z)
                z_sum = self.X_SYSTEM + tr.X + worst.fault_z
                i_f = u0 / (SQRT3 * z_sum)  # кА
                u_bus = u0 * worst.fault_z / z_sum
                for f in flt:
                    f.I = i_f * 1000 * self.rng.uniform(0.98, 1.02) if f is worst else 0.0
                    s_f = SQRT3 * u_bus * f.I / 1000
                    f.P, f.Q = 0.15 * s_f, 0.99 * s_f
                    sec_p[f.section] += f.P
                    sec_q[f.section] += f.Q
                    P += f.P
                    Q += f.Q
                tr.U_lv = u_bus
            else:
                tr.U_lv = u0 - (P * tr.R + Q * tr.X) / u0
            tr.P_lv, tr.Q_lv = P, Q
            tr.I_lv = math.hypot(P, Q) / (SQRT3 * tr.U_lv)  # кА
            tr.K = tr.I_lv / tr.I_NOM_LV
            tr.P = P + tr.PK * tr.K ** 2 + tr.P0
            tr.Q = Q + tr.UK * tr.S_NOM * tr.K ** 2 + tr.I0 * tr.S_NOM
            tr.I_hv = math.hypot(tr.P, tr.Q) / (SQRT3 * tr.U_hv) * 1000  # А

        # Напряжения секций и токи присоединений
        for s in (1, 2):
            u = self.trafos[src[s]].U_lv if src[s] is not None else 0.0
            self.sec_u[s] = u
            self.sec_i[s] = math.hypot(sec_p[s], sec_q[s]) / (SQRT3 * u) * 1000 if u > 0 else 0.0
        for f in self.feeders.values():
            if f.energized and not f.sim_fault:
                u = self.sec_u[f.section]
                f.I = math.hypot(f.P, f.Q) / (SQRT3 * u) * 1000 if u > 0 else 0.0

    def _aux_load(self) -> tuple[float, float]:
        """Собственные нужды ПС, МВт / Мвар: защиты и связь, заряд ЩПТ, освещение, обогрев, обдув Т1/Т2."""
        h = self.clock.hour + self.clock.minute / 60
        light = 0.004 if h < 7 or h >= 19 else 0.0
        heating = 0.0012 * max(0.0, 10.0 - self.ambient)  # обогрев ЗРУ, шкафов и приводов
        fans = 0.004 * sum(tr.fans for tr in self.trafos.values())
        p = (0.016 + light + heating + fans) * (1 + self.aux_noise.x)
        return p, 0.45 * p + 0.003

    def _metering(self, dt_sim: float) -> None:
        """Счётчики интегрируют мощность в модельном времени."""
        hours = dt_sim / 3600
        for n, tr in self.trafos.items():
            self.meters[f"T{n}"].add(tr.P, tr.Q, hours)
            self.meters[f"In{n}"].add(tr.P_lv, tr.Q_lv, hours)
        for tag, f in self.feeders.items():
            self.meters[tag].add(f.P, f.Q, hours)
        for s in (1, 2):
            self.meters[f"Aux{s}"].add(self.aux_p[s], self.aux_q[s], hours)

    def _protection(self, dt: float) -> None:
        for f in self.feeders.values():
            if f.cb.closed and not f.cb.moving and f.I > f.iset:
                f.prot_timer += dt
                if f.prot_timer >= f.tset:
                    f.cb.operate(False)
                    f.trip = True
                    f.prot_timer = 0.0
                    self._force_analog = True
                    self._event(SEV_ALARM, f"{f.title}: отключение от МТЗ, I = {f.I:.0f} А")
            else:
                f.prot_timer = 0.0

    def _abr(self, dt: float) -> None:
        """АВР 10 кВ: при потере питания секции отключить её ввод и включить СВ."""
        if self._abr_section is not None:
            s = self._abr_section
            incb = self.incb[s]
            if incb.moving or incb.closed:
                return
            self._abr_section = None
            if not self.seccb.closed and not self.seccb.moving and self._interlock(self.seccb, True) is None:
                self.seccb.operate(True)
                self._event(SEV_ALARM, f"АВР 10 кВ: включение СВ, {SECTIONS[s]} переведена на Т{3 - s}")
            return

        for s in (1, 2):
            o = 3 - s
            start = (
                self.abr_enabled and not self.abr_operated
                and self.sec_src[s] is None and not self.trafos[s].energized
                and self.incb[s].closed and not self.seccb.closed
                and self.sec_src[o] == o
            )
            self._abr_timer[s] = self._abr_timer[s] + dt if start else 0.0
            if start and self._abr_timer[s] >= self.abr_delay:
                self._abr_timer[s] = 0.0
                self.abr_operated = True
                self._abr_section = s
                self.incb[s].operate(False)
                self._event(SEV_ALARM, f"АВР 10 кВ: потеря питания {SECTIONS[s]}, отключение ввода")
                return

    def _start_tap(self, tr: Transformer, direction: int, by: str) -> None:
        new = tr.tap + direction
        if not tr.TAP_MIN <= new <= tr.TAP_MAX:
            if not tr.tap_limit_reported:
                tr.tap_limit_reported = True
                self._event(SEV_WARNING, f"{tr.title}: РПН в крайнем положении {tr.tap} ({by})")
            return
        tr.tap_limit_reported = False
        tr.tap_dir = direction
        tr.tap_remaining = tr.TAP_TIME * self.rng.uniform(0.9, 1.1)

    def _tap_changer(self, tr: Transformer, dt: float) -> None:
        if tr.tap_dir:
            tr.tap_remaining -= dt
            if tr.tap_remaining <= 0:
                tr.tap += tr.tap_dir
                self._event(SEV_INFO, f"{tr.title}: РПН {'прибавить' if tr.tap_dir > 0 else 'убавить'}, "
                                      f"положение {tr.tap}")
                tr.tap_dir = 0
                self._force_analog = True
            return
        if not (tr.avr_auto and tr.energized and self.aux):
            tr.avr_timer = 0.0
            return
        # Блокировка АРНТ при КЗ / глубокой просадке и при токе выше 1.5 Iном
        if tr.U_lv < 0.8 * tr.uset or tr.K > 1.5:
            tr.avr_timer = 0.0
            return
        dev = (tr.U_lv - tr.uset) / tr.uset * 100
        if abs(dev) <= tr.deadband:
            tr.avr_timer = 0.0
            return
        tr.avr_timer += dt
        if tr.avr_timer >= tr.delay:
            tr.avr_timer = 0.0
            self._start_tap(tr, 1 if dev < 0 else -1, "АРНТ")

    def _cooling(self, tr: Transformer, dt: float) -> None:
        # Пуск обдува по току — с выдержкой 60 с, чтобы не реагировать на КЗ и броски
        tr.fan_load_timer = tr.fan_load_timer + dt if tr.K >= 0.9 else 0.0
        was = tr.fans
        if not self.aux:
            tr.fans = False
        elif not tr.fans and (tr.oil >= tr.fan_on_temp or tr.fan_load_timer >= 60):
            tr.fans = True
        elif tr.fans and tr.oil < tr.fan_on_temp - 5 and tr.K < 0.8:
            tr.fans = False
        if tr.fans != was:
            self._event(SEV_INFO, f"{tr.title}: обдув {'включен' if tr.fans else 'отключен'}")

    def _station(self, dt: float) -> None:
        dt_sim = dt * self.time_scale
        self.ambient = ambient_base(self.clock) + self.t_noise.step(dt)
        self.f_slow.step(dt)
        self.f_fast.step(dt)
        any_line = any(ln.U > 0 for ln in self.lines.values())
        self.frequency = 50.0 + self.f_slow.x + self.f_fast.x if any_line else 0.0

        aux = any(self.sec_u[s] > 0.3 * U_NOM_10 for s in (1, 2))
        if aux != self.aux:
            self._event(SEV_ALARM if not aux else SEV_INFO,
                        f"Питание собственных нужд {'восстановлено' if aux else 'потеряно, ЩПТ на аккумуляторной батарее'}")
            if not aux:
                self.battery = min(self.battery, 219.0)
        self.aux = aux
        target, tau = (232.0, 600.0) if aux else (205.0, 3 * 3600.0)
        self.battery += (target - self.battery) * (1 - math.exp(-dt_sim / tau))

    def _alarms(self, dt: float) -> None:
        for n, ln in self.lines.items():
            on = ln.U > 0
            if on != self._line_u_prev[n]:
                self._event(SEV_INFO if on else SEV_WARNING,
                            f"{ln.title}: напряжение {'появилось' if on else 'исчезло'}")
                self._line_u_prev[n] = on
        for s in (1, 2):
            on = self.sec_u[s] > 0
            if on != self._sec_u_prev[s]:
                self._event(SEV_INFO if on else SEV_ALARM,
                            f"{SECTIONS[s]}: напряжение {'восстановлено' if on else 'исчезло'}")
                self._sec_u_prev[s] = on
        for tr in self.trafos.values():
            alarm = tr.oil >= tr.oil_alarm_set or (tr.oil_alarm and tr.oil >= tr.oil_alarm_set - 2)
            if alarm != tr.oil_alarm:
                tr.oil_alarm = alarm
                self._event(SEV_ALARM if alarm else SEV_INFO,
                            f"{tr.title}: повышение температуры масла {'— сигнал' if alarm else 'снято'}"
                            f" ({tr.oil:.1f} °C)")
            if tr.K > 1.05:
                tr.overload_timer += dt
            else:
                tr.overload_timer = 0.0
            overload = tr.overload_timer >= 10 or (tr.overload and tr.K >= 1.0)
            if overload != tr.overload:
                tr.overload = overload
                self._event(SEV_WARNING if overload else SEV_INFO,
                            f"{tr.title}: перегрузка {'— сигнал' if overload else 'снята'} ({tr.K * 100:.0f}%)")

    def _publish(self, analog: bool) -> None:
        v = self.values
        for prefix, sw in self.switches.items():
            v[f"{prefix}.State"] = sw.state
        v["Station.RemoteMode"] = self.remote
        v["ABR.Operated"] = self.abr_operated
        for n, tr in self.trafos.items():
            p = f"T{n}"
            v[f"{p}.Tap.Position"] = tr.tap
            v[f"{p}.Tap.InProgress"] = tr.tap_dir != 0
            v[f"{p}.Cooling.FansOn"] = tr.fans
            v[f"{p}.Alarm.OilTemp"] = tr.oil_alarm
            v[f"{p}.Alarm.Overload"] = tr.overload
        for tag, f in self.feeders.items():
            v[f"{tag}.Prot.Trip"] = f.trip
        v["Station.GeneralAlarm"] = (
            any(f.trip for f in self.feeders.values()) or self.abr_operated
            or any(tr.oil_alarm for tr in self.trafos.values())
            or any(u <= 0 for u in self.sec_u.values())
        )
        v["Station.GeneralWarning"] = (
            any(tr.overload for tr in self.trafos.values()) or self.battery < 210
            or any(ln.U <= 0 for ln in self.lines.values()) or not self.remote
        )
        v["Station.Heartbeat"] = int(self._uptime) & 0xFFFFFFFF
        if not analog:
            return

        v["Station.SimTime"] = f"{self.clock:%Y-%m-%d %H:%M:%S}"
        v["Station.AmbientTemp"] = round(self.ambient, 1)
        v["Station.Frequency"] = round(self.frequency, 3)
        v["Station.BatteryVoltage"] = round(self.battery + self.rng.gauss(0, 0.05), 1)
        for n, ln in self.lines.items():
            v[f"VL{n}.U"] = round(ln.U, 1)
        for n, tr in self.trafos.items():
            p = f"T{n}"
            v[f"{p}.P"] = round(tr.P, 3)
            v[f"{p}.Q"] = round(tr.Q, 3)
            v[f"{p}.I_HV"] = round(tr.I_hv, 1)
            v[f"{p}.U_LV"] = round(tr.U_lv, 2)
            v[f"{p}.Load"] = round(tr.K * 100, 1)
            v[f"{p}.OilTemp"] = round(tr.oil, 1)
            v[f"{p}.WindingTemp"] = round(tr.winding, 1)
        for s in (1, 2):
            v[f"Sec{s}.U"] = round(self.sec_u[s], 2)
            src = self.sec_src[s]
            fed_own = self.incb[s].closed and src == s
            v[f"Sec{s}.InCB.I"] = round(self.trafos[s].I_lv * 1000, 1) if fed_own else 0.0
        via_seccb = [s for s in (1, 2) if self.seccb.closed and self.sec_src[s] == 3 - s]
        v["SecCB.I"] = round(self.sec_i[via_seccb[0]], 1) if via_seccb else 0.0
        for tag, f in self.feeders.items():
            v[f"{tag}.P"] = round(f.P, 3)
            v[f"{tag}.Q"] = round(f.Q, 3)
            v[f"{tag}.I"] = round(f.I, 1)
        for key, m in self.meters.items():
            v[f"Meter.{key}.Aplus"] = round(m.a, 2)
            v[f"Meter.{key}.Rplus"] = round(m.r, 2)
