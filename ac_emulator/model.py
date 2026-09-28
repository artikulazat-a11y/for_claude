"""Модель кондиционеров (не зависит от Modbus).

Каждый кондиционер — сплит-система с инверторным компрессором, которая
обслуживает своё помещение. Помещение нагревается от людей и техники, обменивается
теплом с улицей и соседними помещениями. Кондиционер по уставке охлаждает или
греет воздух: ПИ-регулятор задаёт частоту компрессора, скорость вентилятора в режиме
«авто» зависит от того, насколько температура далека от уставки.

Модель считается шагами ``Fleet.step(dt)``; записи клиентов приходят через
``AirConditioner.write(name, value)``. Результат — словарь ``values`` у каждого
кондиционера (имя регистра -> значение в физических единицах).
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from .registers import (
    ALARMS, AUTO, COOL, FAN_MODES, FAN_ONLY, HEAT, MODES, REGISTERS_BY_NAME, SETTINGS, ST_ALARM, ST_COOL, ST_FAN,
    ST_HEAT, ST_IDLE, ST_OFF, alarm_text,
)

SEV_INFO, SEV_WARNING, SEV_ALARM = 200, 500, 800

# Ошибки записи (имена исключений Modbus)
ILLEGAL_DATA_ADDRESS = "ILLEGAL_DATA_ADDRESS"
ILLEGAL_DATA_VALUE = "ILLEGAL_DATA_VALUE"

# Среднемесячная температура воздуха (средняя полоса), °C
MONTHLY_MEAN_TEMP = [-7, -6, -1, 6, 13, 17, 19, 17, 11, 5, -1, -5]

DAY = 24 * 3600.0


def outdoor_base(t: datetime) -> float:
    """Температура наружного воздуха без случайной составляющей: сезон + суточный ход (макс. в 15:00)."""
    m = t.month - 1
    frac = (t.day - 1) / 30
    mean = MONTHLY_MEAN_TEMP[m] * (1 - frac) + MONTHLY_MEAN_TEMP[(m + 1) % 12] * frac
    h = t.hour + t.minute / 60
    return mean + 5.0 * math.cos(2 * math.pi * (h - 15) / 24)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _approach(x: float, target: float, up: float, down: float) -> float:
    """Движение к цели с ограниченной скоростью (up/down — максимальный шаг вверх/вниз)."""
    return min(target, x + up) if target > x else max(target, x - down)


class OU:
    """Случайный процесс Орнштейна-Уленбека: «живой» шум со среднеквадратичным sigma и памятью tau, с."""

    def __init__(self, rng: random.Random, sigma: float, tau: float):
        self.rng, self.sigma, self.tau = rng, sigma, tau
        self.x = rng.gauss(0, sigma)

    def step(self, dt: float) -> float:
        a = math.exp(-dt / self.tau)
        self.x = self.x * a + self.sigma * math.sqrt(1 - a * a) * self.rng.gauss(0, 1)
        return self.x


class Site:
    """Общие для всех кондиционеров модельное время и погода."""

    def __init__(self, rng: random.Random, start: datetime):
        self.clock = start
        self.noise = OU(rng, 0.8, 1800)
        self.outdoor = outdoor_base(start) + self.noise.x

    def step(self, dt_sim: float) -> None:
        self.clock += timedelta(seconds=dt_sim)
        self.outdoor = outdoor_base(self.clock) + self.noise.step(dt_sim)

    @property
    def occupied(self) -> bool:
        """Рабочее время: в помещениях люди и включена техника."""
        return self.clock.weekday() < 5 and 8 <= self.clock.hour < 19

    @property
    def building(self) -> float:
        """Температура в соседних помещениях здания: зимой отопление, летом прогрев."""
        return 21.0 + 0.3 * max(0.0, self.outdoor - 18.0)


class AirConditioner:
    """Инверторная сплит-система и помещение, которое она обслуживает."""

    F_MIN, F_NOM, F_MAX = 20.0, 70.0, 90.0  # Гц: минимальная, паспортная производительность, максимальная
    RAMP_UP, RAMP_DOWN = 1.0, 3.0  # Гц/с
    RESTART_DELAY = 60.0  # с: защита компрессора от частых пусков
    FAN_RPM = {0: 0, 1: 650, 2: 900, 3: 1150}
    FAN_RAMP = 150.0  # об/мин за секунду
    KP, TI = 0.35, 300.0  # ПИ-регулятор частоты компрессора: о.е./°C, с
    THERMO_ON, THERMO_OFF = 0.5, 0.8  # °C: пуск компрессора выше уставки на 0,5, останов при переохлаждении на 0,8
    CHANGEOVER = 1.5  # °C: в режиме «авто» переход охлаждение <-> нагрев
    FAN_STEP = {2: 1.0, 3: 2.0}  # °C: авто-скорость — средняя / высокая при отклонении от уставки больше
    FAN_HYST = 0.3  # °C
    REACHED = 1.0  # °C: «уставка достигнута»

    def __init__(self, number: int, site: Site, rng: random.Random,
                 emit: Callable[[int, int, str], None] = lambda *_: None):
        self.number = number
        self.title = f"Кондиционер №{number}"
        self.site, self.rng, self._emit = site, rng, emit
        # Паспорт и помещение зависят только от номера: не меняются между запусками
        p = random.Random(f"ac-{number}")
        self.capacity = p.choice([2.5, 3.5, 3.5, 5.0, 5.0, 7.0])  # кВт холода
        k = self.capacity
        self.heat_capacity = 70.0 * k * p.uniform(0.8, 1.2)  # кДж/°C: воздух, мебель, внутренние стены
        self.ua_out = 0.008 * k * p.uniform(0.7, 1.3)  # кВт/°C: теплопередача наружу
        self.ua_in = 0.03 * k * p.uniform(0.7, 1.3)  # кВт/°C: к соседним помещениям и отоплению
        self.gain = k * p.uniform(0.2, 0.45)  # кВт: тепловыделения в рабочее время (ночью вдвое меньше)
        self.rpm_k = p.uniform(0.97, 1.03)
        self.outdoor_offset = p.gauss(0, 0.2)  # погрешность датчика наружного воздуха
        # Уставки
        self.power = True
        self.mode = AUTO
        self.setpoint = 22.0
        self.fan_mode = 0
        self.dirty = False  # уставки или авария изменились — сохранить
        # Аварии
        self.alarm = 0
        self.last_alarm = 0
        self.alarm_count = 0
        self.next_alarm: float | None = None  # время следующей случайной аварии, UNIX time
        # Режим
        self.heating = False
        self.compressor = False
        self.freq = 0.0
        self.integral = 0.0
        self.restart_timer = 0.0
        self.waiting = False  # компрессор нужен, но ждёт окончания задержки пуска
        self.fan_level = 0
        self.fan_rpm = 0.0
        self.room = 22.0
        self.supply = 22.0
        self.q_ac = 0.0  # кВт тепла в помещение (< 0 — охлаждение)
        self.power_w = 0.0
        self.run_hours = p.uniform(1500, 20000)
        self.energy = self.run_hours * k * p.uniform(0.15, 0.25)  # кВт·ч
        self.sensor_noise = OU(rng, 0.03, 30)
        self.power_noise = OU(rng, 0.015, 20)
        self.values: dict[str, float] = {}

    # ------------------------------------------------------------------ API

    @property
    def running(self) -> bool:
        return self.power and not self.alarm

    @property
    def state(self) -> int:
        if self.alarm:
            return ST_ALARM
        if not self.power:
            return ST_OFF
        if self.mode == FAN_ONLY:
            return ST_FAN
        if self.compressor:
            return ST_HEAT if self.heating else ST_COOL
        return ST_IDLE

    @property
    def setpoint_reached(self) -> bool:
        return self.running and self.mode != FAN_ONLY and abs(self.room - self.setpoint) <= self.REACHED

    def check(self, name: str, value: object) -> tuple[object, str | None]:
        """Проверка записи: (значение, None или имя исключения Modbus)."""
        reg = REGISTERS_BY_NAME.get(name)
        if reg is None or not reg.writable:
            return value, ILLEGAL_DATA_ADDRESS
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return value, ILLEGAL_DATA_VALUE
        if reg.scale == 1.0:
            if value != int(value):
                return value, ILLEGAL_DATA_VALUE
            value = int(value)
        else:
            value = round(float(value), 1)
        if not reg.lo <= value <= reg.hi:
            return value, ILLEGAL_DATA_VALUE
        return value, None

    def write(self, name: str, value: object) -> str | None:
        """Запись от клиента. Возвращает None или имя исключения Modbus."""
        value, err = self.check(name, value)
        if err:
            return err
        if name == "AlarmReset":
            if value:
                self.reset_alarm()
        elif name == "SimAlarm":
            if value:
                self.raise_alarm(value, simulated=True)
        elif name == "Power":
            if bool(value) != self.power:
                self.power = bool(value)
                if not self.power:
                    self._stop_compressor()
                    self.fan_level = 0
                note = ""
                if self.power and self.alarm:
                    note = f" (активна авария {alarm_text(self.alarm)}, кондиционер запустится после сброса)"
                self._event(SEV_INFO, f"команда «{'Включить' if value else 'Выключить'}»{note}")
                self.dirty = True
        elif name == "Mode":
            if value != self.mode:
                self.mode = value
                if value == FAN_ONLY:
                    self._stop_compressor()
                self._event(SEV_INFO, f"режим «{MODES[value]}»")
                self.dirty = True
        elif name == "Setpoint":
            if value != self.setpoint:
                self.setpoint = value
                self._event(SEV_INFO, f"уставка {value:.1f} °C")
                self.dirty = True
        elif name == "FanMode":
            if value != self.fan_mode:
                self.fan_mode = value
                self._event(SEV_INFO, f"скорость вентилятора «{FAN_MODES[value]}»")
                self.dirty = True
        self._publish()
        return None

    def raise_alarm(self, code: int, simulated: bool = False) -> None:
        if self.alarm:
            if simulated:
                self._event(SEV_WARNING, f"имитация аварии {alarm_text(code)} не выполнена: "
                                         f"уже активна авария {alarm_text(self.alarm)}")
            return
        self.alarm = self.last_alarm = code
        self.alarm_count = min(self.alarm_count + 1, 0xFFFF)
        self._stop_compressor()
        self.fan_level = 0
        self.dirty = True
        self._event(SEV_ALARM, f"АВАРИЯ {alarm_text(code)}{' (имитация)' if simulated else ''}")

    def reset_alarm(self) -> None:
        if not self.alarm:
            return
        self._event(SEV_INFO, f"сброс аварии {alarm_text(self.alarm)}")
        self.alarm = 0
        self.dirty = True

    def step(self, dt: float, dt_real: float | None = None) -> None:
        """Шаг модели: dt — модельное время, с; dt_real — реальное (для шума измерений)."""
        dt_real = dt if dt_real is None else dt_real
        self.restart_timer = max(0.0, self.restart_timer - dt)
        self.sensor_noise.step(dt_real)
        self.waiting = False
        demand = 0.0  # загрузка компрессора, о.е.
        fan = 0
        if not self.running:
            self._stop_compressor()
        elif self.mode == FAN_ONLY:
            self._stop_compressor()
            fan = self.fan_mode or 2
        else:
            heating = self._direction()
            if heating != self.heating:
                self._stop_compressor()  # перекладка четырёхходового клапана — только на остановленном компрессоре
                self.heating = heating
            e = self._error()
            if self.compressor and e < -self.THERMO_OFF:
                self._stop_compressor()
            elif not self.compressor and e > self.THERMO_ON:
                if self.restart_timer > 0:
                    self.waiting = True
                else:
                    self.compressor = True
                    self.freq = self.F_MIN
            if self.compressor:
                # Интегрируем только вне насыщения, иначе после большого отклонения — перерегулирование
                u = self.KP * e + self.integral
                if 0.0 < u < 1.0 or (u >= 1.0) != (e > 0):
                    self.integral = _clamp(self.integral + self.KP * e * dt / self.TI, 0.0, 1.0)
                demand = _clamp(self.KP * e + self.integral, 0.0, 1.0)
            fan = self.fan_mode or self._auto_fan(e)

        if self.compressor:
            target = self.F_MIN + demand * (self.F_MAX - self.F_MIN)
            self.freq = _approach(self.freq, target, self.RAMP_UP * dt, self.RAMP_DOWN * dt)
        else:
            self.freq = 0.0
        self.fan_level = fan
        rpm = self.FAN_RPM[fan] * self.rpm_k
        self.fan_rpm = _approach(self.fan_rpm, rpm, self.FAN_RAMP * dt, self.FAN_RAMP * dt)

        flow = self._flow()
        q = self._output(self.freq, flow) if self.compressor else 0.0
        self.q_ac = q if self.heating else -q
        self.room += (self._heat_gain(self.room) + self.q_ac) / self.heat_capacity * dt
        self._supply_air(flow, dt)
        self._electric(q, flow, dt, dt_real)

    def init_steady_state(self) -> None:
        """Стартуем из установившегося режима: температура у уставки, компрессор на нужной частоте."""
        self.freq, self.integral, self.compressor = 0.0, 0.0, False
        self.restart_timer, self.waiting = 0.0, False
        q_need = self._heat_gain(self.setpoint)  # > 0 — помещение нагревается, нужен холод
        heating = q_need < 0
        idle = (self.mode == COOL and heating) or (self.mode == HEAT and not heating)
        if not self.running or self.mode == FAN_ONLY or idle:
            # Кондиционеру нечего делать: помещение пришло к своей равновесной температуре
            self.room = self._equilibrium()
            self.heating = self.mode == HEAT
            if not self.running:
                self.fan_level = 0
            elif self.mode == FAN_ONLY:
                self.fan_level = self.fan_mode or 2
            else:
                self.fan_level = self.fan_mode or 1
        else:
            self.heating = heating
            self.fan_level = self.fan_mode or 1
            per_hz = self._output(1.0, self.FAN_RPM[self.fan_level] / self.FAN_RPM[3])
            f = abs(q_need) / per_hz
            if f >= self.F_MIN:
                self.compressor = True
                self.freq = min(f, self.F_MAX)
                self.integral = (self.freq - self.F_MIN) / (self.F_MAX - self.F_MIN)
                self.room = self.setpoint + self.rng.gauss(0, 0.15) * (-1 if heating else 1)
            else:
                # Малая нагрузка: компрессор работает циклами пуск/останов
                self.compressor = self.rng.random() < f / self.F_MIN
                self.freq = self.F_MIN if self.compressor else 0.0
                e = self.rng.uniform(-self.THERMO_OFF, self.THERMO_ON)
                self.room = self.setpoint - e if heating else self.setpoint + e
        self.fan_rpm = self.FAN_RPM[self.fan_level] * self.rpm_k
        flow = self._flow()
        q = self._output(self.freq, flow) if self.compressor else 0.0
        self.q_ac = q if self.heating else -q
        self._supply_air(flow, 1e6)
        self._electric(q, flow, 0.0, 0.0)
        self._publish()

    # ---------------------------------------------------------- внутреннее

    def _event(self, severity: int, text: str) -> None:
        self._emit(self.number, severity, text)

    def _stop_compressor(self) -> None:
        if self.compressor:
            self.compressor = False
            self.restart_timer = self.RESTART_DELAY
        self.freq = self.integral = 0.0

    def _error(self) -> float:
        """Отклонение температуры от уставки в сторону, которую нужно исправлять (> 0 — нужна работа)."""
        return self.setpoint - self.room if self.heating else self.room - self.setpoint

    def _direction(self) -> bool:
        """Действующий режим: True — нагрев, False — охлаждение."""
        if self.mode == COOL:
            return False
        if self.mode == HEAT:
            return True
        if self.heating and self.room > self.setpoint + self.CHANGEOVER:
            return False
        if not self.heating and self.room < self.setpoint - self.CHANGEOVER:
            return True
        return self.heating

    def _auto_fan(self, e: float) -> int:
        level = max(self.fan_level, 1)
        while level < 3 and e > self.FAN_STEP[level + 1]:
            level += 1
        while level > 1 and e < self.FAN_STEP[level] - self.FAN_HYST:
            level -= 1
        return level

    def _flow(self) -> float:
        """Расход воздуха, о.е. от максимального."""
        return self.fan_rpm / (self.FAN_RPM[3] * self.rpm_k)

    def _output(self, freq: float, flow: float) -> float:
        """Тепло- или холодопроизводительность, кВт."""
        out = self.site.outdoor
        derate = 1 - 0.02 * max(0.0, 7.0 - out) if self.heating else 1 - 0.01 * max(0.0, out - 35.0)
        return self.capacity * freq / self.F_NOM * flow ** 0.4 * max(0.5, derate)

    def _heat_gain(self, t_room: float) -> float:
        """Приток тепла в помещение без кондиционера, кВт (< 0 — помещение остывает)."""
        s = self.site
        gain = self.gain if s.occupied else 0.5 * self.gain
        return self.ua_out * (s.outdoor - t_room) + self.ua_in * (s.building - t_room) + gain

    def _equilibrium(self) -> float:
        s = self.site
        gain = self.gain if s.occupied else 0.5 * self.gain
        return (self.ua_out * s.outdoor + self.ua_in * s.building + gain) / (self.ua_out + self.ua_in)

    def _supply_air(self, flow: float, dt: float) -> None:
        airflow = 0.13 * self.capacity * flow  # кВт/°C: теплоёмкость потока воздуха
        if airflow > 0.01:
            target, tau = self.room + self.q_ac / airflow, 40.0
        else:
            target, tau = self.room, 180.0
        target = _clamp(target, 7.0, 55.0)  # ограничено температурой теплообменника
        self.supply += (target - self.supply) * (1 - math.exp(-dt / tau))

    def _electric(self, q: float, flow: float, dt: float, dt_real: float) -> None:
        out = self.site.outdoor
        if q > 0:
            if self.heating:
                cop = _clamp(4.0 + 0.05 * (out - 7.0) - 0.02 * (self.freq - 40.0), 1.8, 5.0)
            else:
                cop = _clamp(4.3 - 0.06 * (out - 20.0) - 0.02 * (self.freq - 40.0), 2.0, 5.5)
            p_comp = q / cop * (1 + self.power_noise.step(dt_real))
        else:
            p_comp = 0.0
        p_fan = 0.045 * self.capacity / 3.5 * flow ** 3
        p_ctrl = 0.010 if self.running else 0.004
        self.power_w = (p_comp + p_fan + p_ctrl) * 1000
        self.energy += self.power_w / 1000 * dt / 3600
        if self.fan_rpm > 0:
            self.run_hours += dt / 3600

    def _publish(self, heartbeat: int | None = None) -> None:
        v = self.values
        v["Power"] = int(self.power)
        v["Mode"] = self.mode
        v["Setpoint"] = self.setpoint
        v["FanMode"] = self.fan_mode
        v["AlarmReset"] = 0
        v["SimAlarm"] = 0
        state = self.state
        v["State"] = state
        running, reached = self.running, self.setpoint_reached
        v["Status"] = (
            int(self.power) | running << 1 | self.compressor << 2 | bool(self.alarm) << 3 | reached << 4
            | (running and self.waiting) << 5
            | (running and self.mode != FAN_ONLY and self.heating) << 6
        )
        v["RoomTemp"] = round(self.room + self.sensor_noise.x, 1)
        v["SupplyTemp"] = round(self.supply, 1)
        v["OutdoorTemp"] = round(self.site.outdoor + self.outdoor_offset, 1)
        v["FanSpeed"] = round(self.fan_rpm)
        v["FanLevel"] = self.fan_level
        v["CompressorFreq"] = round(self.freq)
        v["PowerInput"] = round(self.power_w)
        v["AlarmCode"] = self.alarm
        v["LastAlarmCode"] = self.last_alarm
        v["AlarmCount"] = self.alarm_count
        v["Energy"] = self.energy
        v["RunHours"] = math.floor(self.run_hours)
        if heartbeat is not None:
            v["Heartbeat"] = heartbeat
        v.setdefault("Heartbeat", 0)
        v["DeviceNumber"] = self.number
        v["Capacity"] = round(self.capacity * 1000)
        # Дискретные входы
        v["Running"] = running
        v["Compressor"] = self.compressor
        v["Alarm"] = bool(self.alarm)
        v["SetpointReached"] = reached


class Fleet:
    """Все кондиционеры эмулятора: общая погода, случайные аварии, сохранение состояния."""

    MIN_ALARM_INTERVAL = DAY  # случайная авария у одного кондиционера — не чаще раза в сутки
    MAX_STEP = 2.0  # с модельного времени: при ускорении шаг делится, чтобы регулятор не раскачивался

    def __init__(self, count: int = 50, time_scale: float = 1.0, seed: int | None = None,
                 start: datetime | None = None, random_alarms: bool = True, alarm_period_hours: float = 72.0,
                 clock: Callable[[], float] = time.time):
        self.rng = random.Random(seed)
        self.time_scale = time_scale
        self.clock = clock
        self.site = Site(self.rng, start or datetime.now())
        self.events: list[tuple[int, int, str]] = []  # (номер кондиционера, важность, текст)
        self.units = {n: AirConditioner(n, self.site, self.rng, self._emit) for n in range(1, count + 1)}
        self.random_alarms = random_alarms
        self.alarm_period = max(alarm_period_hours * 3600, self.MIN_ALARM_INTERVAL)
        self._uptime = 0.0
        now = self.clock()
        for u in self.units.values():
            u.next_alarm = now + self.rng.uniform(0, self.alarm_period)
            u.init_steady_state()

    @property
    def settings_dirty(self) -> bool:
        return any(u.dirty for u in self.units.values())

    def pop_events(self) -> list[tuple[int, int, str]]:
        ev, self.events = self.events, []
        return ev

    def step(self, dt: float) -> None:
        now = self.clock()
        self._uptime += dt
        dt_sim = dt * self.time_scale
        n = max(1, math.ceil(dt_sim / self.MAX_STEP))
        for _ in range(n):
            self.site.step(dt_sim / n)
            for u in self.units.values():
                u.step(dt_sim / n, dt / n)
        if self.random_alarms:
            self._random_alarms(now)
        heartbeat = int(self._uptime) & 0xFFFF
        for u in self.units.values():
            u._publish(heartbeat)

    def state(self) -> dict[str, dict[str, object]]:
        """Сохраняемое состояние: уставки, аварии, счётчики (как энергонезависимая память)."""
        data = {}
        for n, u in self.units.items():
            d: dict[str, object] = {name: u.values[name] for name in SETTINGS}
            d.update(AlarmCode=u.alarm, LastAlarmCode=u.last_alarm, AlarmCount=u.alarm_count,
                     Energy=round(u.energy, 3), RunHours=round(u.run_hours, 4),
                     NextRandomAlarm=None if u.next_alarm is None else round(u.next_alarm))
            data[str(n)] = d
        return data

    def load_state(self, data: dict) -> None:
        now = self.clock()
        for key, d in data.items():
            u = self.units.get(int(key)) if str(key).isdigit() else None
            if u is None or not isinstance(d, dict):
                continue
            for name in SETTINGS:
                if name in d:
                    u.write(name, d[name])

            def number(name: str) -> float | None:
                x = d.get(name)
                ok = isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0
                return float(x) if ok else None

            for name, attr in (("Energy", "energy"), ("RunHours", "run_hours")):
                if number(name) is not None:
                    setattr(u, attr, number(name))
            if number("AlarmCount") is not None:
                u.alarm_count = min(int(number("AlarmCount")), 0xFFFF)
            for name, attr in (("AlarmCode", "alarm"), ("LastAlarmCode", "last_alarm")):
                code = d.get(name)
                if type(code) is int and (code in ALARMS or code == 0):
                    setattr(u, attr, code)
            t = number("NextRandomAlarm")
            if t is not None:
                # Срок уже прошёл (эмулятор был остановлен) — назначаем заново, как при первом запуске.
                # Иначе оставляем: интервал от прошлой случайной аварии уже не меньше суток.
                u.next_alarm = now + self.rng.uniform(0, self.alarm_period) if t < now \
                    else min(t, now + self.alarm_period)
        self.events.clear()
        for u in self.units.values():
            u.dirty = False
            u.init_steady_state()

    # ---------------------------------------------------------- внутреннее

    def _emit(self, number: int, severity: int, text: str) -> None:
        self.events.append((number, severity, text))

    def _random_alarms(self, now: float) -> None:
        for u in self.units.values():
            if u.next_alarm is None or now < u.next_alarm:
                continue
            if u.alarm:
                # Прошлая авария ещё не сброшена — новая случится позже
                u.next_alarm = now + self.rng.uniform(1800, 3 * 3600)
                continue
            codes = [code for code, (_s, _t, needs_run) in ALARMS.items() if u.running or not needs_run]
            u.raise_alarm(self.rng.choice(codes))
            u.next_alarm = now + self.rng.uniform(self.MIN_ALARM_INTERVAL, self.alarm_period)
