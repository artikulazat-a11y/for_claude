"""Имитатор случайных неисправностей ПВУ-1.

Неисправности приходят по одной, со случайным интервалом (экспоненциальное
распределение со средним `interval` секунд модели). Каждая длится случайное
время, затем «ремонтируется». Аварии и блокировки из-за неё выставляет
автоматика модели, как на реальном щите.
"""


# имя: (вес, длительность мин–макс в с, текст)
SCENARIOS = {
    "supply_fan_belt": (3, (60, 180), "Обрыв ремня приточного вентилятора"),
    "exhaust_fan_trip": (3, (60, 150), "Сработала защита двигателя вытяжного вентилятора"),
    "pump_trip": (2, (60, 150), "Отказ циркуляционного насоса M 2е"),
    "heat_loss": (2, (120, 300), "Пропал теплоноситель в тепловой сети"),
    "valve_stuck": (2, (180, 360), "Заклинило привод клапана M 1в"),
    "filter_clog": (3, (150, 300), "Интенсивное засорение фильтра"),
    "room_gains": (2, (240, 420), "Большие теплопритоки в помещении"),
    "local_key": (2, (60, 150), "Ключ GKS 6ж переведён в «Местное»"),
}


class FaultInjector:
    def __init__(self, model, rng, log, interval=180.0, enabled=True):
        self.model = model
        self.rng = rng
        self.log = log
        self.interval = interval
        self.enabled = enabled
        self.active = None      # имя активной неисправности
        self.left = 0.0         # сколько ей осталось
        self.next_in = self._draw_pause(first=True)

    def _draw_pause(self, first=False):
        pause = self.rng.expovariate(1.0 / self.interval)
        return max(30.0 if first else 20.0, pause)

    @property
    def text(self):
        return SCENARIOS[self.active][2] if self.active else ""

    def step(self, dt):
        if self.active:
            self.left -= dt
            if self.left <= 0:
                self.clear()
            return
        if not self.enabled:
            return
        self.next_in -= dt
        if self.next_in <= 0:
            names = list(SCENARIOS)
            weights = [SCENARIOS[n][0] for n in names]
            self.inject(self.rng.choices(names, weights)[0])

    def inject(self, name, duration=None):
        if name not in SCENARIOS:
            raise ValueError(f"неизвестная неисправность: {name}; есть: {', '.join(SCENARIOS)}")
        if self.active:
            self.clear()
        lo, hi = SCENARIOS[name][1]
        self.active = name
        self.left = duration if duration else self.rng.uniform(lo, hi)
        m, f = self.model, self.model.faults
        if name == "supply_fan_belt":
            f.fan_sa_belt = True
        elif name == "exhaust_fan_trip":
            f.fan_ea_trip = True
        elif name == "pump_trip":
            f.pump_trip = True
        elif name == "heat_loss":
            f.heat_loss = True
        elif name == "valve_stuck":
            f.valve_stuck = self.rng.choice([self.rng.uniform(0.1, 0.2), self.rng.uniform(0.85, 1.0)])
            m.v = f.valve_stuck
        elif name == "filter_clog":
            f.filter_rate = 40.0
        elif name == "room_gains":
            f.extra_gains = self.rng.uniform(0.13, 0.17)
        elif name == "local_key":
            m.key_sa_local = True
        self.log("warn", f"Имитация: {SCENARIOS[name][2]} (≈{self.left:.0f} с)")

    def clear(self):
        if not self.active:
            return
        name, m, f = self.active, self.model, self.model.faults
        f.fan_sa_belt = f.fan_ea_trip = f.pump_trip = f.heat_loss = False
        f.valve_stuck = None
        f.extra_gains = 0.0
        if name == "filter_clog":
            f.filter_rate = 1.0
        if name == "local_key":
            m.key_sa_local = False
            self.log("info", "Ключ GKS 6ж возвращён в «Дистанционное»")
        else:
            self.log("ok", f"Имитация завершена: {SCENARIOS[name][2]}")
        self.active = None
        self.next_in = self._draw_pause()
