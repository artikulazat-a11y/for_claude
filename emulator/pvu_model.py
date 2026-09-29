"""Модель процесса и автоматики ПВУ-1.

Физика упрощённая, но связанная: наружный воздух, заслонки, вентиляторы,
водяной калорифер со смесительным узлом (насос + трёхходовой клапан),
помещение. Автоматика повторяет щит GCS 6а: последовательный пуск,
ПИ-регулятор температуры в помещении, защита от замерзания, блокировки
по перепаду на вентиляторах. Время везде в секундах модели.
"""

import math
from collections import defaultdict
from dataclasses import dataclass


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def approach(cur, target, step):
    if cur < target:
        return min(target, cur + step)
    return max(target, cur - step)


def lag(cur, target, dt, tau):
    """Инерционное звено первого порядка."""
    return cur + (target - cur) * (1.0 - math.exp(-dt / tau))


class State:
    STOP, START, RUN, STOPPING, TRIP, FREEZE = range(6)
    TEXT = {
        0: "Стоп",
        1: "Пуск",
        2: "Работа",
        3: "Останов",
        4: "Авария",
        5: "Защита от замерзания",
    }


@dataclass
class Faults:
    """Неисправности, которые включает имитатор аварий."""
    fan_sa_belt: bool = False      # обрыв ремня: двигатель крутится, колесо стоит
    fan_ea_trip: bool = False      # сработала защита двигателя вытяжного вентилятора
    pump_trip: bool = False        # отказ циркуляционного насоса
    heat_loss: bool = False        # пропал теплоноситель в сети
    valve_stuck: float | None = None  # клапан заклинило в этом положении (0..1)
    filter_rate: float = 1.0       # множитель скорости засорения фильтра
    extra_gains: float = 0.0       # дополнительные теплопритоки в помещение, °C/с


ALARMS = ("alarm_filter", "alarm_fan_sa", "alarm_fan_ea", "alarm_freeze", "alarm_water", "alarm_room")
ALARM_TEXT = {
    "alarm_filter": "Засорение фильтра (PDA 3б)",
    "alarm_fan_sa": "Нет перепада на приточном вентиляторе (PDA 5б)",
    "alarm_fan_ea": "Нет перепада на вытяжном вентиляторе (PDA 4б)",
    "alarm_freeze": "Угроза замерзания калорифера (TS 2в)",
    "alarm_water": "Низкая температура обратной воды (TIS 2г)",
    "alarm_room": "Температура в помещении вне допуска (TICAS 1б)",
}


class PVUModel:
    # уставки защит и регулятора
    FILTER_ALARM = 2.5     # мбар, PDA 3б
    FAN_DP_MIN = 0.5       # мбар, PDA 4б / PDA 5б
    FAN_CHECK_DELAY = 15   # с после пуска вентилятора до контроля перепада
    FREEZE_T = 5.0         # °C, TS 2в
    WATER_T = 20.0         # °C, TIS 2г
    ROOM_BAND = 3.0        # °C, допуск TICAS 1б
    ROOM_DELAY = 120       # с вне допуска до аварии
    FAN_SPEED = 0.85       # задание частоты вентиляторов в работе

    def __init__(self, rng, log, auto_reset=60.0):
        self.rng = rng
        self.log = log                 # log(level, text), level: info | warn | alarm | ok
        self.auto_reset = auto_reset   # с; 0 — сброс только оператором
        self.faults = Faults()
        self.t = 0.0

        # оператор
        self.sp = 21.0
        self.key_sa_local = False
        self.key_ea_local = False
        self.want_run = False

        # процесс
        self.out = -6.0
        self.t_net = 75.0     # температура подачи из теплосети
        self.room = 19.5
        self.air_h = 16.0     # воздух за калорифером
        self.ret = 36.0       # обратная вода
        self.twc = 45.0       # вода на входе в калорифер (после насоса)
        self.v = 0.3          # положение клапана, 0..1
        self.integ = 0.0
        self.d_oa = 0.0
        self.d_ea = 0.0
        self.sa = 0.0         # фактическая скорость, 0..1
        self.ea = 0.0
        self.sa_on = False    # команда на двигатель
        self.ea_on = False
        self.pump_on = True
        self.pump_flow = 1.0
        self.filter_k = 1.2   # перепад на фильтре при номинальном расходе, мбар
        self.filter_dp = 0.0
        self.fan_sa_dp = 0.0
        self.fan_ea_dp = 0.0
        self.pressure = 2.4

        # автоматика
        self.state = State.STOP
        self.state_t = 0.0
        self.trip_t = 0.0
        self.sa_run_t = 0.0
        self.ea_run_t = 0.0
        self.alarms = {k: False for k in ALARMS}
        self.timers = defaultdict(float)
        self.filter_alarm_t = 0.0
        self.noise = {}
        self.out_phase = rng.uniform(0, 2 * math.pi)

    # ---------------- команды оператора ----------------
    def command(self, name, value=None):
        if name == "start":
            if self.interlock:
                self.log("warn", "Пуск запрещён: активна блокировка, нужен сброс аварий")
                self.want_run = True
                return False
            self.want_run = True
            if self.state in (State.STOP, State.STOPPING):
                self._goto(State.START)
                self.log("info", "Команда: пуск установки")
            return True
        if name == "stop":
            self.want_run = False
            if self.state in (State.START, State.RUN):
                self._goto(State.STOPPING)
                self.log("info", "Команда: останов установки")
            return True
        if name == "reset":
            return self.reset(manual=True)
        if name == "setpoint":
            self.sp = round(clamp(float(value), 16.0, 28.0), 1)
            self.log("info", f"Уставка температуры: {self.sp:.1f} °C")
            return True
        if name == "replace_filter":
            self.filter_k = self.rng.uniform(0.85, 1.0)
            self.faults.filter_rate = 1.0
            self.log("ok", "Фильтр заменён")
            return True
        return False

    def reset(self, manual):
        """Сброс защёлкнутых аварий, если причина ушла."""
        who = "оператором" if manual else "автоматически"
        cleared = []
        for k in ("alarm_fan_sa", "alarm_fan_ea"):
            if self.alarms[k]:
                self._set_alarm(k, False)
                cleared.append(k)
        if self.alarms["alarm_freeze"] and self.air_h > self.FREEZE_T + 3:
            self._set_alarm("alarm_freeze", False)
            cleared.append("alarm_freeze")
        if self.alarms["alarm_water"] and self.ret > self.WATER_T + 5:
            self._set_alarm("alarm_water", False)
            cleared.append("alarm_water")
        if self.state in (State.TRIP, State.FREEZE) and not self._trip_active():
            self._goto(State.STOP)
            self.log("ok", f"Блокировка снята {who}")
            if self.want_run:
                self._goto(State.START)
                self.log("info", "Повторный пуск после сброса")
            return True
        if manual and not cleared:
            self.log("warn", "Сброс: причина аварии не устранена")
        return bool(cleared)

    # ---------------- служебное ----------------
    @property
    def interlock(self):
        return self.state in (State.TRIP, State.FREEZE)

    def _trip_active(self):
        return self.alarms["alarm_fan_sa"] or self.alarms["alarm_fan_ea"] or self.alarms["alarm_freeze"]

    def _goto(self, st):
        self.state = st
        self.state_t = 0.0
        if st in (State.TRIP, State.FREEZE):
            self.trip_t = 0.0

    def _set_alarm(self, key, on):
        if self.alarms[key] == on:
            return
        self.alarms[key] = on
        if on:
            self.log("alarm", ALARM_TEXT[key])
        else:
            self.log("ok", f"Устранено: {ALARM_TEXT[key]}")

    def _delay(self, key, cond, dt, secs):
        """True, если условие держится secs секунд."""
        self.timers[key] = self.timers[key] + dt if cond else 0.0
        return self.timers[key] >= secs

    def _noise(self, key, amp, dt, tau=3.0):
        """Плавный шум для правдоподобных показаний."""
        n = self.noise.get(key, 0.0)
        n = lag(n, self.rng.uniform(-amp, amp), dt, tau)
        self.noise[key] = n
        return n

    # ---------------- шаг модели ----------------
    def step(self, dt):
        self.t += dt
        self.state_t += dt
        self.trip_t += dt
        f = self.faults

        # --- наружный воздух и теплосеть ---
        out_target = -6.0 + 5.0 * math.sin(2 * math.pi * self.t / 1800.0 + self.out_phase)
        self.out = lag(self.out, out_target, dt, 60) + self._noise("out", 0.02, dt)
        net_target = 12.0 if f.heat_loss else clamp(70.0 + max(0.0, -self.out) * 0.8, 60.0, 95.0)
        self.t_net = lag(self.t_net, net_target, dt, 40)

        # --- автоматика: задания на исполнительные механизмы ---
        st = self.state
        d_cmd, sa_cmd, ea_cmd = 0.0, False, False
        pump_cmd = self.out < 5.0 or st in (State.START, State.RUN, State.FREEZE)
        standby_v = clamp(0.15 + (30.0 - self.ret) * 0.05)
        v_cmd = standby_v

        if st == State.STOP:
            if self.want_run and not self.interlock:
                self._goto(State.START)
        elif st == State.START:
            d_cmd = 1.0
            preheat = self.state_t < 20 and self.ret < 35
            v_cmd = 1.0 if preheat else self._pid(dt)
            ea_cmd = not preheat and self.d_ea > 0.9
            sa_cmd = ea_cmd and self.ea_run_t > 5 and self.d_oa > 0.9
            if sa_cmd and self.sa > 0.8 * self.FAN_SPEED:
                self._goto(State.RUN)
                self.log("ok", "Установка вышла на режим")
        elif st == State.RUN:
            d_cmd, sa_cmd, ea_cmd = 1.0, True, True
            v_cmd = self._pid(dt)
        elif st == State.STOPPING:
            ea_cmd = self.state_t < 5
            d_cmd = 1.0 if (self.sa > 0.1 or self.ea > 0.1) else 0.0
            if self.sa < 0.02 and self.ea < 0.02 and self.d_oa < 0.02:
                self._goto(State.STOP)
                self.log("info", "Установка остановлена")
        elif st == State.FREEZE:
            v_cmd, pump_cmd = 1.0, True

        # ключи «Местное»: вентилятор не слушает дистанционные команды, защиты действуют
        if self.key_sa_local and not self.interlock:
            sa_cmd = self.sa_on
        if self.key_ea_local and not self.interlock:
            ea_cmd = self.ea_on

        # --- исполнительные механизмы ---
        self.d_oa = approach(self.d_oa, d_cmd, dt / 15.0)
        self.d_ea = approach(self.d_ea, d_cmd, dt / 15.0)
        self.sa_on = sa_cmd
        self.ea_on = ea_cmd and not f.fan_ea_trip
        self.sa = approach(self.sa, self.FAN_SPEED if self.sa_on else 0.0, dt / 8.0)
        self.ea = approach(self.ea, self.FAN_SPEED if self.ea_on else 0.0, dt / 8.0)
        self.sa_run_t = self.sa_run_t + dt if self.sa_on else 0.0
        self.ea_run_t = self.ea_run_t + dt if self.ea_on else 0.0
        self.pump_on = pump_cmd and not f.pump_trip
        self.pump_flow = lag(self.pump_flow, 1.0 if self.pump_on else 0.0, dt, 3)
        if f.valve_stuck is None:
            self.v = approach(self.v, v_cmd, dt / 25.0)

        # --- физика ---
        wheel_sa = 0.0 if f.fan_sa_belt else self.sa
        q_sa = (wheel_sa / self.FAN_SPEED) * min(1.0, self.d_oa / 0.6)
        pf = self.pump_flow

        if pf > 0.1:
            twc_target = self.ret * (1 - self.v) + self.t_net * self.v
            self.twc = lag(self.twc, twc_target, dt, 5)
        else:
            self.twc = lag(self.twc, self.air_h, dt, 25)
        if q_sa > 0.05:
            air_target = self.out + (self.twc - self.out) * 0.55
        else:
            air_target = 0.65 * self.twc + 0.35 * self.out
        self.air_h = lag(self.air_h, air_target, dt, 4)
        if pf > 0.1:
            ret_target = self.twc - (self.twc - self.out) * 0.2 * q_sa - 2.0
            self.ret = lag(self.ret, ret_target, dt, 6)
        else:
            self.ret = lag(self.ret, (self.air_h + self.twc) / 2, dt, 30)
        gains = 0.01 + f.extra_gains
        self.room += (0.006 * q_sa * (self.air_h - self.room) + 0.0008 * (self.out - self.room) + gains) * dt

        if q_sa > 0.3:
            self.filter_k = min(4.8, self.filter_k + 0.0004 * f.filter_rate * dt)

        # --- показания датчиков ---
        self.filter_dp = max(0.0, self.filter_k * q_sa ** 2 + self._noise("fdp", 0.02, dt))
        self.fan_sa_dp = max(0.0, 3.1 * (wheel_sa / self.FAN_SPEED) ** 2 * (0.93 + 0.07 * min(1.0, self.d_oa / 0.6)) + self._noise("sdp", 0.03, dt))
        self.fan_ea_dp = max(0.0, 2.6 * (self.ea / self.FAN_SPEED) ** 2 + self._noise("edp", 0.03, dt))
        if wheel_sa < 0.05:
            self.fan_sa_dp = max(0.0, self.fan_sa_dp * 0.3)
        self.pressure = 1.9 + 0.5 * pf + self._noise("p", 0.02, dt)

        self._check_alarms(dt)

    def _pid(self, dt):
        """TICAS 1б: ПИ по температуре в помещении и ограничители против замерзания."""
        e = self.sp - self.room
        self.integ = clamp(self.integ + 0.0012 * e * dt, -0.4, 0.6)
        v = clamp(0.35 + 0.2 * e + self.integ)
        if self.air_h < 10:
            v = max(v, clamp(0.6 + (10 - self.air_h) * 0.08))
        if self.ret < 25:
            v = max(v, clamp(0.5 + (25 - self.ret) * 0.05))
        return v

    def _check_alarms(self, dt):
        # PDA 3б — засорение фильтра, держится до замены
        if self._delay("filter", self.filter_dp > self.FILTER_ALARM, dt, 5):
            self._set_alarm("alarm_filter", True)
        if self.alarms["alarm_filter"] and self.filter_k < self.FILTER_ALARM - 0.2:
            self._set_alarm("alarm_filter", False)

        # PDA 5б / PDA 4б — нет перепада на работающем вентиляторе
        if self._delay("fan_sa", self.sa_on and self.sa_run_t > self.FAN_CHECK_DELAY and self.fan_sa_dp < self.FAN_DP_MIN, dt, 5):
            self._set_alarm("alarm_fan_sa", True)
        ea_cmd_lost = self.faults.fan_ea_trip and self.state in (State.START, State.RUN) and self.d_ea > 0.9
        if self._delay("fan_ea", (self.ea_on and self.ea_run_t > self.FAN_CHECK_DELAY and self.fan_ea_dp < self.FAN_DP_MIN) or ea_cmd_lost, dt, 5):
            self._set_alarm("alarm_fan_ea", True)

        # TS 2в — угроза замерзания
        if self._delay("freeze", self.air_h < self.FREEZE_T, dt, 2):
            self._set_alarm("alarm_freeze", True)

        # TIS 2г — низкая температура обратной воды
        if self._delay("water", self.ret < self.WATER_T, dt, 5):
            self._set_alarm("alarm_water", True)
        if self.alarms["alarm_water"] and self.ret > self.WATER_T + 5 and not self.interlock:
            self._set_alarm("alarm_water", False)

        # TICAS 1б — отклонение от уставки в работе
        dev = abs(self.room - self.sp)
        if self._delay("room", self.state == State.RUN and dev > self.ROOM_BAND, dt, self.ROOM_DELAY):
            self._set_alarm("alarm_room", True)
        if self.alarms["alarm_room"] and (dev < self.ROOM_BAND - 1 or self.state != State.RUN):
            self._set_alarm("alarm_room", False)

        # блокировки GCS 6а
        if self.alarms["alarm_freeze"] and self.state != State.FREEZE:
            self._goto(State.FREEZE)
            self.log("alarm", "Защита от замерзания: вентиляторы остановлены, заслонки закрыты, клапан открыт")
        elif (self.alarms["alarm_fan_sa"] or self.alarms["alarm_fan_ea"]) and self.state not in (State.TRIP, State.FREEZE):
            self._goto(State.TRIP)
            self.log("alarm", "Аварийный останов установки")

        # автоматический сброс, если причина ушла
        if self.auto_reset > 0 and self.interlock and self.trip_t >= self.auto_reset:
            if self._causes_gone():
                self.reset(manual=False)
            self.trip_t = self.auto_reset * 0.5  # следующая попытка через половину интервала

        # засорённый фильтр «меняет» сервисная служба, если авария висит долго
        self.filter_alarm_t = self.filter_alarm_t + dt if self.alarms["alarm_filter"] else 0.0
        if self.auto_reset > 0 and self.filter_alarm_t > max(240.0, self.auto_reset * 3):
            self.command("replace_filter")

    def _causes_gone(self):
        f = self.faults
        if self.alarms["alarm_fan_sa"] and f.fan_sa_belt:
            return False
        if self.alarms["alarm_fan_ea"] and f.fan_ea_trip:
            return False
        if self.alarms["alarm_freeze"] and (self.air_h <= self.FREEZE_T + 3 or f.pump_trip or f.heat_loss):
            return False
        return True

    # ---------------- снимок для OPC UA ----------------
    def snapshot(self):
        a = self.alarms
        return {
            "room_temp": round(self.room, 2),
            "room_sp": self.sp,
            "outdoor_temp": round(self.out, 2),
            "alarm_room": a["alarm_room"],
            "damper_oa_pos": round(self.d_oa * 100, 1),
            "filter_dp": round(self.filter_dp, 3),
            "fan_sa_run": self.sa_on,
            "fan_sa_speed": round(self.sa * 100, 1),
            "fan_sa_dp": round(self.fan_sa_dp, 3),
            "key_sa_local": self.key_sa_local,
            "alarm_filter": a["alarm_filter"],
            "alarm_fan_sa": a["alarm_fan_sa"],
            "damper_ea_pos": round(self.d_ea * 100, 1),
            "fan_ea_run": self.ea_on,
            "fan_ea_speed": round(self.ea * 100, 1),
            "fan_ea_dp": round(self.fan_ea_dp, 3),
            "key_ea_local": self.key_ea_local,
            "alarm_fan_ea": a["alarm_fan_ea"],
            "valve_pos": round(self.v * 100, 1),
            "pump_run": self.pump_on,
            "water_supply_temp": round(self.twc, 2),
            "water_return_temp": round(self.ret, 2),
            "water_pressure": round(self.pressure, 3),
            "air_heater_temp": round(self.air_h, 2),
            "alarm_freeze": a["alarm_freeze"],
            "alarm_water": a["alarm_water"],
            "unit_run": self.state in (State.START, State.RUN),
            "unit_state": State.TEXT[self.state],
            "unit_state_code": self.state,
            "interlock": self.interlock,
            "alarm_count": sum(1 for v in a.values() if v),
        }
