"""Физическая модель кондиционера (не зависит от Modbus).

Помещение с теплопотерями на улицу, инверторная сплит-система: скорость
вентилятора и производительность компрессора плавно подстраиваются под
разницу между температурой в помещении и уставкой. Не чаще раза в модельные
сутки случается авария, которая останавливает агрегат до сброса.

Модель считается шагами ``step(dt)`` (dt — реальные секунды, умножаются на
``time_scale``); команды приходят через методы ``set_*`` / ``reset_fault``.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta

log = logging.getLogger("ac_emulator")

# Режимы работы
MODE_AUTO, MODE_COOL, MODE_HEAT, MODE_FAN = 0, 1, 2, 3
MODE_NAMES = {MODE_AUTO: "авто", MODE_COOL: "охлаждение", MODE_HEAT: "обогрев", MODE_FAN: "вентиляция"}
# Скорость вентилятора: авто или ручная ступень
FAN_AUTO, FAN_LOW, FAN_MED, FAN_HIGH = 0, 1, 2, 3
FAN_STEP_PERCENT = {FAN_LOW: 35.0, FAN_MED: 65.0, FAN_HIGH: 100.0}

# Состояние агрегата
STATE_OFF, STATE_IDLE, STATE_COOLING, STATE_HEATING, STATE_FAN, STATE_FAULT = 0, 1, 2, 3, 4, 5

# Коды аварий
FAULTS = {
    1: "перегрев компрессора",
    2: "высокое давление хладагента",
    3: "низкое давление хладагента (утечка)",
    4: "отказ двигателя вентилятора",
    5: "обрыв датчика температуры в помещении",
}

SETPOINT_MIN, SETPOINT_MAX = 16.0, 30.0
FAULT_MIN_INTERVAL = 24 * 3600.0  # случайная авария не чаще раза в сутки, модельные секунды

FAN_RAMP = 5.0            # скорость изменения оборотов вентилятора, %/с
FAN_MIN_AUTO = 30.0       # обороты в авто при достигнутой уставке, %
FAN_RPM_MAX = 1200.0
COMPRESSOR_RAMP = 4.0     # скорость изменения производительности компрессора, %/с
HYSTERESIS = 0.5          # зона нечувствительности вокруг уставки, °C
CHANGEOVER = 2.0          # режим «авто»: переход охлаждение <-> обогрев при отклонении, °C
COMPRESSOR_MIN = 20.0     # минимальная производительность инверторного компрессора, %
ROOM_TAU = 3600.0         # постоянная времени теплопотерь помещения, с
AC_RATE = 0.01            # скорость охлаждения/нагрева на полной мощности, °C/с (0.6 °C/мин)
SUPPLY_TAU = 20.0         # инерция температуры приточного воздуха, с
P_FAN_MAX, P_COMPRESSOR_MAX, P_STANDBY = 60.0, 2400.0, 5.0  # потребляемая мощность, Вт


class ACModel:
    def __init__(self, time_scale: float = 1.0, seed: int | None = None, start: datetime | None = None,
                 faults_enabled: bool = True, room_temp: float = 27.0):
        self.time_scale = time_scale
        self.rng = random.Random(seed)
        self.now = start or datetime.now()
        self.faults_enabled = faults_enabled

        # Команды / уставки
        self.power = False
        self.setpoint = 22.0
        self.mode = MODE_AUTO
        self.fan_mode = FAN_AUTO

        # Состояние
        self.room_temp = room_temp
        self.outdoor_temp = self._outdoor_at(self.now)
        self.supply_temp = room_temp
        self.fan_percent = 0.0
        self.compressor_percent = 0.0
        self.active = 0            # 1 — охлаждение, -1 — обогрев, 0 — компрессор не нужен
        self.last_active = 0       # последнее направление работы (для режима «авто»)
        self.fault_code = 0
        self.fault_count = 0
        self.last_fault_at: datetime | None = None
        self.next_fault_at = self.now + timedelta(seconds=self.rng.uniform(3600.0, FAULT_MIN_INTERVAL))
        self.noise = 0.0

    # ---------------------------------------------------------------- команды
    def set_power(self, on: bool) -> None:
        if bool(on) != self.power:
            self.power = bool(on)
            log.info("Кондиционер %s", "включён" if on else "выключен")

    def set_setpoint(self, value: float) -> None:
        if not SETPOINT_MIN <= value <= SETPOINT_MAX:
            raise ValueError(f"уставка {value} вне диапазона {SETPOINT_MIN}..{SETPOINT_MAX} °C")
        if value != self.setpoint:
            self.setpoint = value
            log.info("Уставка температуры: %.1f °C", value)

    def set_mode(self, mode: int) -> None:
        if mode not in MODE_NAMES:
            raise ValueError(f"неизвестный режим {mode}")
        if mode != self.mode:
            self.mode = mode
            self.active = 0
            log.info("Режим: %s", MODE_NAMES[mode])

    def set_fan_mode(self, fan_mode: int) -> None:
        if fan_mode not in (FAN_AUTO, FAN_LOW, FAN_MED, FAN_HIGH):
            raise ValueError(f"неизвестная скорость вентилятора {fan_mode}")
        if fan_mode != self.fan_mode:
            self.fan_mode = fan_mode
            log.info("Скорость вентилятора: %s", "авто" if fan_mode == FAN_AUTO else fan_mode)

    def reset_fault(self) -> bool:
        """Сброс аварии. Возвращает True, если авария была."""
        if not self.fault_code:
            return False
        log.info("Авария %d сброшена", self.fault_code)
        self.fault_code = 0
        return True

    def trigger_fault(self, code: int, scheduled: bool = False) -> None:
        if code not in FAULTS:
            raise ValueError(f"неизвестный код аварии {code}")
        self.fault_code = code
        self.fault_count += 1
        self.active = 0
        log.warning("АВАРИЯ %d: %s%s", code, FAULTS[code], "" if scheduled else " (тестовая, от клиента)")

    # ---------------------------------------------------------------- расчёт
    @staticmethod
    def _outdoor_at(t: datetime) -> float:
        """Суточный ход уличной температуры: минимум 18 °C в 5 утра, максимум 30 °C в 17 часов."""
        hours = t.hour + t.minute / 60 + t.second / 3600
        return 24.0 - 6.0 * math.cos((hours - 5.0) / 24.0 * 2 * math.pi)

    @property
    def running(self) -> bool:
        return self.power and not self.fault_code

    @property
    def state(self) -> int:
        if self.fault_code:
            return STATE_FAULT
        if not self.power:
            return STATE_OFF
        if self.mode == MODE_FAN:
            return STATE_FAN
        return {1: STATE_COOLING, -1: STATE_HEATING}.get(self.active, STATE_IDLE)

    @property
    def fan_rpm(self) -> float:
        return self.fan_percent / 100.0 * FAN_RPM_MAX

    @property
    def power_watts(self) -> float:
        if not self.power:
            return 0.0
        return P_STANDBY + P_FAN_MAX * self.fan_percent / 100 + P_COMPRESSOR_MAX * self.compressor_percent / 100

    def _update_demand(self) -> None:
        """Нужен ли компрессор и в какую сторону (с гистерезисом)."""
        err = self.room_temp - self.setpoint
        if self.active and err * self.active <= -HYSTERESIS:
            self.active = 0  # уставка достигнута с небольшим перебегом
        if self.active:
            return
        if self.mode == MODE_COOL:
            self.active = 1 if err > HYSTERESIS else 0
        elif self.mode == MODE_HEAT:
            self.active = -1 if err < -HYSTERESIS else 0
        else:
            # Авто: продолжить в прежнем направлении — от гистерезиса, сменить направление — от CHANGEOVER
            cool_at = HYSTERESIS if self.last_active != -1 else CHANGEOVER
            heat_at = HYSTERESIS if self.last_active != 1 else CHANGEOVER
            if err > cool_at:
                self.active = 1
            elif err < -heat_at:
                self.active = -1
        if self.active:
            self.last_active = self.active

    def _targets(self) -> tuple[float, float]:
        """Целевые обороты вентилятора и производительность компрессора, %."""
        if not self.running:
            return 0.0, 0.0
        if self.mode == MODE_FAN:
            self.active = 0
            fan = 50.0 if self.fan_mode == FAN_AUTO else FAN_STEP_PERCENT[self.fan_mode]
            return fan, 0.0
        self._update_demand()
        # Инвертор: чем дальше от уставки, тем выше мощность; у уставки — минимальная
        err = (self.room_temp - self.setpoint) * self.active
        compressor = min(100.0, max(COMPRESSOR_MIN, COMPRESSOR_MIN + 40.0 * err)) if self.active else 0.0
        fan_auto = max(FAN_MIN_AUTO, compressor)
        fan = fan_auto if self.fan_mode == FAN_AUTO else FAN_STEP_PERCENT[self.fan_mode]
        # На низкой ручной скорости вентилятора компрессор ограничен теплосъёмом
        return fan, min(compressor, fan)

    @staticmethod
    def _ramp(value: float, target: float, rate: float, dt: float) -> float:
        step = rate * dt
        return target if abs(target - value) <= step else value + math.copysign(step, target - value)

    def _check_random_fault(self) -> None:
        if not self.faults_enabled or self.now < self.next_fault_at:
            return
        if self.running and self.mode != MODE_FAN:
            self.last_fault_at = self.now
            self.trigger_fault(self.rng.choice(list(FAULTS)), scheduled=True)
            base = self.now + timedelta(seconds=FAULT_MIN_INTERVAL)
        else:
            base = self.now  # агрегат не работал — авария переносится, интервал от прошлой сохраняется
        self.next_fault_at = base + timedelta(seconds=self.rng.uniform(0.0, FAULT_MIN_INTERVAL))

    def step(self, dt: float) -> None:
        """Шаг на dt реальных секунд (модельное время — dt * time_scale)."""
        total = dt * self.time_scale
        while total > 1e-9:
            h = min(total, 1.0)
            self._substep(h)
            total -= h

    def _substep(self, h: float) -> None:
        self.now += timedelta(seconds=h)
        self.outdoor_temp = self._outdoor_at(self.now)
        self._check_random_fault()

        fan_t, comp_t = self._targets()
        self.fan_percent = self._ramp(self.fan_percent, fan_t, FAN_RAMP, h)
        self.compressor_percent = self._ramp(self.compressor_percent, comp_t, COMPRESSOR_RAMP, h)
        load = self.compressor_percent / 100.0
        sign = self.active if self.compressor_percent > 0 else 0

        # Помещение: теплообмен с улицей + работа кондиционера
        self.room_temp += ((self.outdoor_temp - self.room_temp) / ROOM_TAU - sign * AC_RATE * load) * h
        # Приточный воздух: холоднее/теплее комнатного пропорционально нагрузке
        supply_target = self.room_temp + {1: -12.0, -1: 18.0}.get(sign, 0.0) * load
        if self.fan_percent < 1.0:
            supply_target = self.room_temp
        self.supply_temp += (supply_target - self.supply_temp) * min(1.0, h / SUPPLY_TAU)
        # Небольшой шум показаний датчиков (случайное блуждание в пределах ±0.1 °C)
        self.noise = max(-0.1, min(0.1, self.noise + self.rng.gauss(0.0, 0.01) * math.sqrt(h)))

    # Показания датчиков с шумом
    @property
    def room_temp_measured(self) -> float:
        return self.room_temp + self.noise
