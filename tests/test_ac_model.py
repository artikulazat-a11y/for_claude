"""Тесты модели кондиционеров (без Modbus)."""

from datetime import datetime

from ac_emulator.model import DAY, ILLEGAL_DATA_ADDRESS, ILLEGAL_DATA_VALUE, SEV_ALARM, Fleet
from ac_emulator.registers import (
    ALARMS, COILS, DISCRETE_INPUTS, REGISTER_AT, REGISTER_COUNT, REGISTERS, REGISTERS_BY_NAME, ST_ALARM, ST_COOL,
    ST_FAN, ST_HEAT, ST_IDLE, ST_OFF, WRITE, encode_registers,
)

T0 = 1_800_000_000.0


def make(start=datetime(2026, 7, 15, 14, 0), count=3, **kw):
    return Fleet(count=count, seed=kw.pop("seed", 3), start=start, random_alarms=kw.pop("random_alarms", False), **kw)


def run(fleet, seconds, dt=1.0):
    for _ in range(round(seconds / dt)):
        fleet.step(dt)


def test_register_map_consistent():
    assert len(REGISTER_AT) == sum(r.size for r in REGISTERS)  # регистры не перекрываются
    f = make()
    for u in f.units.values():
        for r in REGISTERS:
            assert r.name in u.values, r.name
        for b in COILS + DISCRETE_INPUTS:
            assert b.name in u.values, b.name
        assert len(encode_registers(u.values)) == REGISTER_COUNT
        for r in REGISTERS:
            if r.access == WRITE:
                assert u.values[r.name] == r.default, r.name


def test_encoding_signed_scaled_and_uint32():
    t = REGISTERS_BY_NAME["RoomTemp"]
    assert t.encode(-12.3) == [0x10000 - 123]
    assert t.decode(0x10000 - 123) == -12.3
    e = REGISTERS_BY_NAME["Energy"]
    assert e.encode(123456.7) == [1234567 >> 16, 1234567 & 0xFFFF]


def test_initial_state_is_steady():
    f = make(count=50)
    v = f.units[1].values
    run(f, 60)
    states = {u.values["State"] for u in f.units.values()}
    assert states <= {ST_COOL, ST_IDLE}  # летом в режиме «авто» все охлаждают или ждут
    for u in f.units.values():
        assert abs(u.room - 22.0) < 1.5
        assert u.values["DeviceNumber"] == u.number
    assert v["Heartbeat"] == 60


def test_cooling_follows_setpoint():
    f = make()
    u = f.units[1]
    u.write("Setpoint", 18.0)
    run(f, 120)
    v = u.values
    assert v["State"] == ST_COOL
    assert v["FanLevel"] == 3 and v["FanSpeed"] > 1000
    assert v["CompressorFreq"] > 70
    assert v["SupplyTemp"] < v["RoomTemp"] - 5
    assert v["PowerInput"] > 1000
    run(f, 1800)
    v = u.values
    assert abs(u.room - 18.0) < 0.7
    assert v["FanLevel"] == 1 and v["FanSpeed"] < 700  # у уставки вентилятор сбавляет обороты
    assert v["CompressorFreq"] < 60
    assert v["SetpointReached"]


def test_heating_in_winter_auto_mode():
    f = make(start=datetime(2026, 1, 15, 22, 0))
    u = f.units[2]
    assert u.values["OutdoorTemp"] < 0
    u.write("Setpoint", 26.0)
    run(f, 120)
    assert u.values["State"] == ST_HEAT
    assert u.values["SupplyTemp"] > u.values["RoomTemp"] + 5
    assert u.values["Status"] & 1 << 6  # действующий режим — нагрев
    run(f, 1800)
    assert abs(u.room - 26.0) < 0.7


def test_auto_mode_changes_over_to_cooling():
    f = make(start=datetime(2026, 1, 15, 22, 0))
    u = f.units[1]
    run(f, 10)
    assert u.heating
    u.write("Setpoint", 16.0)  # в помещении стало на 6 °C теплее уставки
    run(f, 120)
    assert u.values["State"] == ST_COOL
    u.write("Mode", 2)  # «нагрев»: при температуре выше уставки компрессору делать нечего
    run(f, 10)
    assert u.values["State"] == ST_IDLE and u.values["CompressorFreq"] == 0


def test_power_off_and_on():
    f = make()
    u = f.units[1]
    u.write("Power", 0)
    run(f, 20)
    v = u.values
    assert v["State"] == ST_OFF
    assert v["FanSpeed"] == 0 and v["CompressorFreq"] == 0 and v["PowerInput"] < 10
    assert not v["Running"]
    t_off = u.room
    run(f, 1800)
    assert u.room > t_off + 1  # без кондиционера помещение нагревается
    u.write("Power", 1)
    run(f, 60)
    assert u.values["State"] == ST_COOL and u.values["FanLevel"] >= 2


def test_manual_fan_speed_and_fan_only():
    f = make()
    u = f.units[1]
    u.write("FanMode", 3)
    run(f, 15)
    assert u.values["FanLevel"] == 3 and u.values["FanSpeed"] > 1100
    u.write("FanMode", 1)
    run(f, 15)
    assert u.values["FanLevel"] == 1 and 600 < u.values["FanSpeed"] < 700
    u.write("Mode", 3)
    run(f, 15)
    assert u.values["State"] == ST_FAN and u.values["CompressorFreq"] == 0 and u.values["FanSpeed"] > 600


def test_invalid_writes_rejected():
    f = make()
    u = f.units[1]
    assert u.write("Setpoint", 35.0) == ILLEGAL_DATA_VALUE
    assert u.write("Mode", 7) == ILLEGAL_DATA_VALUE
    assert u.write("Mode", 1.5) == ILLEGAL_DATA_VALUE
    assert u.write("SimAlarm", 9) == ILLEGAL_DATA_VALUE
    assert u.write("RoomTemp", 20.0) == ILLEGAL_DATA_ADDRESS
    assert u.values["Setpoint"] == 22.0 and u.values["Mode"] == 0


def test_alarm_stops_unit_until_reset():
    f = make()
    u = f.units[1]
    run(f, 5)
    u.write("SimAlarm", 3)
    v = u.values
    assert v["State"] == ST_ALARM and v["AlarmCode"] == 3 and v["Alarm"]
    assert v["CompressorFreq"] == 0 and v["Power"] == 1  # команда «включить» остаётся
    run(f, 20)
    assert u.values["FanSpeed"] == 0
    assert any(sev == SEV_ALARM and "E3" in text for _n, sev, text in f.pop_events())

    u.write("SimAlarm", 1)  # вторая авария поверх первой не ставится
    assert u.values["AlarmCode"] == 3

    u.write("Power", 0)
    u.write("Power", 1)
    run(f, 5)
    assert u.values["State"] == ST_ALARM  # выключение/включение аварию не сбрасывает

    u.write("Setpoint", 18.0)
    u.write("AlarmReset", 1)
    run(f, 5)
    v = u.values
    assert v["AlarmCode"] == 0 and v["LastAlarmCode"] == 3 and v["AlarmCount"] == 1
    assert v["Running"] and v["FanSpeed"] > 0
    assert v["Status"] & 1 << 5 and v["CompressorFreq"] == 0  # компрессор ждёт окончания задержки пуска
    run(f, 60)
    assert u.values["State"] == ST_COOL and u.values["CompressorFreq"] > 0


def test_random_alarms_not_more_than_once_a_day():
    f = make(count=10, random_alarms=True, alarm_period_hours=24)
    alarms: dict[int, list[float]] = {}
    now = f.clock()
    for _ in range(round(5 * DAY / 60)):  # пять суток с шагом в минуту — только расписание аварий
        now += 60
        f._random_alarms(now)
        for n, sev, _text in f.pop_events():
            if sev == SEV_ALARM:
                alarms.setdefault(n, []).append(now)
                if n % 2:
                    f.units[n].write("AlarmReset", 1)  # у чётных аварию никто не сбрасывает
    assert len(alarms) == 10
    for n, times in alarms.items():
        assert all(b - a >= DAY for a, b in zip(times, times[1:])), n
        if n % 2:
            assert len(times) >= 4  # с периодом 24 ч — каждые сутки
        else:
            assert len(times) == 1  # несброшенная авария не даёт появиться новой


def test_random_alarm_on_stopped_unit_needs_no_running():
    now = [T0]
    f = make(count=20, random_alarms=True, alarm_period_hours=24, clock=lambda: now[0])
    for u in f.units.values():
        u.write("Power", 0)
    now[0] += DAY
    f.step(1.0)
    codes = {u.alarm for u in f.units.values()}
    assert all(u.alarm for u in f.units.values())
    assert all(not ALARMS[c][2] for c in codes), codes


def test_state_roundtrip():
    now = [T0]
    f = make(clock=lambda: now[0])
    u = f.units[2]
    for name, value in (("Power", 0), ("Mode", 2), ("Setpoint", 24.5), ("FanMode", 3), ("SimAlarm", 7)):
        assert u.write(name, value) is None
    run(f, 10)
    u.next_alarm = T0 + 10 * 3600
    f.units[1].next_alarm = T0 - 100  # срок прошёл, пока эмулятор был остановлен
    data = f.state()

    g = make(clock=lambda: now[0], seed=99)
    g.load_state(data)
    w = g.units[2]
    assert (w.power, w.mode, w.setpoint, w.fan_mode) == (False, 2, 24.5, 3)
    assert (w.alarm, w.last_alarm, w.alarm_count) == (7, 7, u.alarm_count)
    assert abs(w.energy - u.energy) < 0.01 and w.capacity == u.capacity
    assert w.next_alarm == T0 + 10 * 3600
    assert g.units[1].next_alarm >= T0
    assert not g.pop_events() and not g.settings_dirty


def test_time_scale_keeps_control_stable():
    f = make(time_scale=600)  # модельные 10 минут за реальную секунду
    u = f.units[1]
    u.write("Setpoint", 18.0)
    run(f, 6, dt=0.2)  # час модельного времени
    assert abs(u.room - 18.0) < 1.0
    assert u.values["State"] in (ST_COOL, ST_IDLE)


def test_load_state_ignores_garbage():
    f = make()
    f.load_state({"1": {"Setpoint": 99, "Mode": "x", "Energy": -5, "AlarmCode": [3], "LastAlarmCode": 42},
                  "abc": {}, "77": {}, "2": 5})
    u = f.units[1]
    assert u.setpoint == 22.0 and u.mode == 0 and u.energy > 0 and u.alarm == 0


def test_registers_csv_up_to_date():
    from pathlib import Path

    from ac_emulator.registers import registers_csv

    saved = (Path(__file__).parent.parent / "ac_registers.csv").read_text(encoding="utf-8-sig")
    assert saved == registers_csv(), "перевыгрузите: python -m ac_emulator --export-registers ac_registers.csv"
