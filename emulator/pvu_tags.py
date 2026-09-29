"""Теги OPC UA эмулятора ПВУ-1.

Имена совпадают с переменными страницы pvu.html (window.PVU.set), поэтому
привязка делается один к одному. NodeId каждого тега: ns=<idx>;s=PVU1.<имя>.
"""

# имя, папка, тип, единицы, описание, запись разрешена
TAGS = [
    # Помещение
    ("room_temp", "Room", "Double", "°C", "Температура в помещении (TC 1а → TICAS 1б)", False),
    ("room_sp", "Room", "Double", "°C", "Уставка температуры в помещении (TICAS 1б), 16–28 °C", True),
    ("outdoor_temp", "Room", "Double", "°C", "Температура наружного воздуха", False),
    ("alarm_room", "Room", "Boolean", "", "Авария: температура в помещении вне допуска (TICAS 1б)", False),

    # Приток
    ("damper_oa_pos", "Supply", "Double", "%", "Заслонка наружного воздуха, положение (M 6з → GS3 6б)", False),
    ("filter_dp", "Supply", "Double", "мбар", "Перепад давления на фильтре (PD 3а → PDA 3б)", False),
    ("fan_sa_run", "Supply", "Boolean", "", "Приточный вентилятор в работе (M 6л → GCS 6д)", False),
    ("fan_sa_speed", "Supply", "Double", "%", "Приточный вентилятор, скорость", False),
    ("fan_sa_dp", "Supply", "Double", "мбар", "Перепад на приточном вентиляторе (PD 5а → PDA 5б)", False),
    ("key_sa_local", "Supply", "Boolean", "", "Ключ GKS 6ж в положении «Местное»", True),
    ("alarm_filter", "Supply", "Boolean", "", "Авария: засорение фильтра (PDA 3б)", False),
    ("alarm_fan_sa", "Supply", "Boolean", "", "Авария: нет перепада на приточном вентиляторе (PDA 5б)", False),

    # Вытяжка
    ("damper_ea_pos", "Exhaust", "Double", "%", "Заслонка выброса, положение (M 6и → GS5 6в)", False),
    ("fan_ea_run", "Exhaust", "Boolean", "", "Вытяжной вентилятор в работе (M 6к → GCS 6г)", False),
    ("fan_ea_speed", "Exhaust", "Double", "%", "Вытяжной вентилятор, скорость", False),
    ("fan_ea_dp", "Exhaust", "Double", "мбар", "Перепад на вытяжном вентиляторе (PD 4а → PDA 4б)", False),
    ("key_ea_local", "Exhaust", "Boolean", "", "Ключ GKS 6е в положении «Местное»", True),
    ("alarm_fan_ea", "Exhaust", "Boolean", "", "Авария: нет перепада на вытяжном вентиляторе (PDA 4б)", False),

    # Калорифер и узел обвязки
    ("valve_pos", "Heater", "Double", "%", "Клапан калорифера, положение (M 1в ← TICAS 1б)", False),
    ("pump_run", "Heater", "Boolean", "", "Циркуляционный насос в работе (M 2е → GCS 2д)", False),
    ("water_supply_temp", "Heater", "Double", "°C", "Прямая вода в калорифер (TI)", False),
    ("water_return_temp", "Heater", "Double", "°C", "Обратная вода (TE 2а → TIS 2г)", False),
    ("water_pressure", "Heater", "Double", "бар", "Давление воды (PI)", False),
    ("air_heater_temp", "Heater", "Double", "°C", "Воздух за калорифером (TE 2б → TS 2в)", False),
    ("alarm_freeze", "Heater", "Boolean", "", "Авария: угроза замерзания калорифера (TS 2в)", False),
    ("alarm_water", "Heater", "Boolean", "", "Авария: низкая температура обратной воды (TIS 2г)", False),

    # Установка (GCS 6а) и диагностика
    ("unit_run", "Unit", "Boolean", "", "Установка в работе (GCS 6а)", False),
    ("unit_state", "Unit", "String", "", "Состояние установки текстом", False),
    ("unit_state_code", "Unit", "Int32", "", "Код состояния: 0 стоп, 1 пуск, 2 работа, 3 останов, 4 авария, 5 защита от замерзания", False),
    ("interlock", "Unit", "Boolean", "", "Блокировка после аварии, нужен сброс", False),
    ("alarm_count", "Unit", "Int32", "", "Число активных аварий", False),
    ("fault_active", "Unit", "String", "", "Имитируемая неисправность (пусто, если нет)", False),
    ("last_event", "Unit", "String", "", "Последнее событие журнала", False),
    ("heartbeat", "Unit", "UInt32", "", "Счётчик циклов эмулятора, растёт каждые 200 мс", False),

    # Команды: запишите true, эмулятор выполнит команду и вернёт false
    ("cmd_start", "Commands", "Boolean", "", "Команда «Пуск»", True),
    ("cmd_stop", "Commands", "Boolean", "", "Команда «Стоп»", True),
    ("cmd_reset", "Commands", "Boolean", "", "Команда «Сброс аварий»", True),
    ("cmd_replace_filter", "Commands", "Boolean", "", "Команда «Фильтр заменён»", True),
]

FOLDERS = {
    "Room": "Помещение",
    "Supply": "Приток",
    "Exhaust": "Вытяжка",
    "Heater": "Калорифер",
    "Unit": "Установка",
    "Commands": "Команды",
}

COMMAND_TAGS = ("cmd_start", "cmd_stop", "cmd_reset", "cmd_replace_filter")
