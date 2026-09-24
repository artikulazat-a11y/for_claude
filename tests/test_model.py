"""Тесты физической модели (без OPC UA)."""

import math
from datetime import datetime

from substation_emulator.model import Model
from substation_emulator.tags import CLOSED, CMD, OPEN, TAGS, WRITE


def make(**kw):
    return Model(seed=kw.pop("seed", 3), start=kw.pop("start", datetime(2026, 9, 24, 19, 0)), **kw)


def run(m, seconds, dt=0.1):
    for _ in range(round(seconds / dt)):
        m.step(dt)


def test_setpoint_defaults_match_model():
    m = make()
    for tag in TAGS:
        if tag.access == WRITE:
            obj, attr = m._setpoints[tag.path]
            assert getattr(obj, attr) == tag.default, tag.path


def test_normal_state_is_realistic():
    m = make()
    run(m, 5)
    v = m.values
    for s in (1, 2):
        assert 10.2 < v[f"Sec{s}.U"] < 10.8
        assert v[f"Sec{s}.InCB.State"] == CLOSED
    assert v["SecCB.State"] == OPEN
    assert 110 < v["VL1.U"] < 121
    assert 20 < v["T1.Load"] < 80
    assert v["T1.WindingTemp"] > v["T1.OilTemp"] > v["Station.AmbientTemp"]
    feeders_i = sum(v[f"F{i}.I"] for i in (1, 2))
    assert abs(v["Sec1.InCB.I"] - feeders_i) / feeders_i < 0.05  # ток ввода ≈ сумма фидеров + СН
    assert not v["Station.GeneralAlarm"]


def test_command_rejected_in_local_mode():
    m = make()
    m.write("Station.RemoteMode", False)
    m.write("F1.CB.CmdOpen", True)
    run(m, 1)
    assert m.values["F1.CB.State"] == CLOSED
    assert "местный" in m.values["Station.LastEvent"]


def test_disconnector_interlock_and_travel_time():
    m = make()
    m.write("VL2.DS.CmdOpen", True)
    run(m, 1)
    assert m.values["VL2.DS.State"] == CLOSED  # под нагрузкой — запрещено

    m.write("VL2.CB.CmdOpen", True)
    run(m, 1)
    assert m.values["VL2.CB.State"] == OPEN
    m.write("VL2.DS.CmdOpen", True)
    run(m, 2)
    assert m.values["VL2.DS.State"] == 0  # привод разъединителя в движении
    run(m, 6)
    assert m.values["VL2.DS.State"] == OPEN


def test_abr_on_line_loss():
    m = make()
    run(m, 1)
    m.write("VL1.SimLoss", True)
    run(m, 1)
    assert m.values["Sec1.U"] == 0.0
    run(m, 3)
    v = m.values
    assert v["Sec1.InCB.State"] == OPEN
    assert v["SecCB.State"] == CLOSED
    assert v["Sec1.U"] > 9.5
    assert v["ABR.Operated"] and v["Station.GeneralAlarm"]
    assert v["T1.P"] == 0.0 and v["T2.Load"] > 60
    assert v["SecCB.I"] > 0

    m.write("Station.AlarmReset", True)
    run(m, 0.5)
    assert not m.values["ABR.Operated"]


def test_abr_disabled():
    m = make()
    m.write("ABR.Enabled", False)
    m.write("VL1.SimLoss", True)
    run(m, 5)
    assert m.values["SecCB.State"] == OPEN
    assert m.values["Sec1.U"] == 0.0


def test_parallel_operation_interlock():
    m = make()
    m.write("SecCB.CmdClose", True)
    run(m, 1)
    assert m.values["SecCB.State"] == OPEN


def test_avr_regulates_voltage_after_setpoint_change():
    m = make()
    run(m, 1)
    tap0 = m.values["T1.Tap.Position"]
    m.write("T1.AVR.Delay", 5.0)
    m.write("T1.AVR.Uset", 10.9)
    run(m, 60)
    assert m.values["T1.Tap.Position"] > tap0
    assert abs(m.values["Sec1.U"] - 10.9) / 10.9 * 100 <= 1.2 + 0.3


def test_manual_tap_only_in_manual_mode():
    m = make()
    run(m, 1)
    tap0 = m.values["T2.Tap.Position"]
    m.write("T2.Tap.CmdRaise", True)
    run(m, 7)
    assert m.values["T2.Tap.Position"] == tap0  # АРНТ в авто — ручная команда отклонена
    m.write("T2.AVR.Auto", False)
    m.write("T2.Tap.CmdRaise", True)
    run(m, 1)
    assert m.values["T2.Tap.InProgress"]
    run(m, 6)
    assert m.values["T2.Tap.Position"] == tap0 + 1


def test_feeder_fault_and_reclose_onto_fault():
    m = make()
    m.write("F2.SimFault", True)
    run(m, 1.5)
    assert m.values["F2.CB.State"] == OPEN and m.values["F2.Prot.Trip"]
    m.write("F2.CB.CmdClose", True)  # включение на устойчивое КЗ — повторное отключение
    run(m, 1.5)
    assert m.values["F2.CB.State"] == OPEN
    m.write("F2.SimFault", False)
    m.write("F2.CB.CmdClose", True)
    run(m, 1)
    assert m.values["F2.CB.State"] == CLOSED and m.values["F2.I"] > 0


def test_out_of_range_and_readonly_writes():
    m = make()
    assert m.write("F1.Prot.Iset", 10) == "BadOutOfRange"
    assert m.write("F1.I", 10) == "BadNotWritable"
    assert m.write("F1.Prot.Tset", "abc") == "BadTypeMismatch"
    assert m.write("F1.Prot.Iset", 450) is None and m.feeders["F1"].iset == 450.0


def test_setpoints_roundtrip():
    m = make()
    m.write("T1.AVR.Uset", 10.7)
    m.write("F4.SimFault", True)  # имитация не сохраняется
    data = m.setpoints()
    assert data["T1.AVR.Uset"] == 10.7 and "F4.SimFault" not in data
    assert not any(t.access == CMD for t in TAGS if t.path in data)
    m2 = make()
    m2.load_setpoints(data)
    assert m2.trafos[1].uset == 10.7


def test_day_run_accelerated_is_stable():
    m = make(time_scale=600, start=datetime(2026, 1, 15, 0, 0))  # сутки за 144 с модели
    loads = []
    for _ in range(1440):
        m.step(0.1)
        v = m.values
        for tag in TAGS:
            if tag.type == "Double":
                assert math.isfinite(v[tag.path]), tag.path
        assert 9.9 < v["Sec1.U"] < 11.1 and 9.9 < v["Sec2.U"] < 11.1
        loads.append(v["T1.Load"])
    assert max(loads) > 1.6 * min(loads)  # суточная неравномерность нагрузки
    assert m.clock.day == 16
