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


def test_state_roundtrip_setpoints_and_meters():
    m = make()
    m.write("T1.AVR.Uset", 10.7)
    m.write("F4.SimFault", True)  # имитация не сохраняется
    run(m, 1)
    data = m.state()
    assert data["T1.AVR.Uset"] == 10.7 and "F4.SimFault" not in data
    assert not any(t.access == CMD for t in TAGS if t.path in data)
    assert data["Meter.F1.Aplus"] > 0 and data["Meter.Aux2.Rplus"] > 0
    m2 = make(seed=99)
    m2.load_state(data)
    assert m2.trafos[1].uset == 10.7
    assert m2.meters["F1"].a == data["Meter.F1.Aplus"]
    m2.load_state({"Meter.F2.Aplus": -5, "Meter.F2.Rplus": True, "Unknown.Tag": 1})  # мусор игнорируется
    assert m2.meters["F2"].a > 0


def test_old_flat_setpoints_file_still_loads():
    m = make()
    m.load_state({"F1.Prot.Iset": 450.0, "ABR.Delay": 3.0})  # формат v1.0.x — только уставки
    assert m.feeders["F1"].iset == 450.0 and m.abr_delay == 3.0


def meter_deltas(m, seconds, dt=1.0):
    before = {k: (mt.a, mt.r) for k, mt in m.meters.items()}
    run(m, seconds, dt)
    return {k: (mt.a - before[k][0], mt.r - before[k][1]) for k, mt in m.meters.items()}


def test_meters_count_up_and_balance():
    m = make(time_scale=60)
    d = meter_deltas(m, 600)  # 10 модельных часов
    assert all(a > 0 and r > 0 for a, r in d.values())
    for s, feeders in ((1, ("F1", "F2")), (2, ("F3", "F4"))):
        supplied = d[f"In{s}"][0]
        consumed = sum(d[f][0] for f in feeders) + d[f"Aux{s}"][0]
        assert abs(supplied - consumed) / supplied < 0.01  # небаланс в пределах погрешности счётчиков
    for n in (1, 2):
        losses = (d[f"T{n}"][0] - d[f"In{n}"][0]) / d[f"T{n}"][0]
        assert 0.002 < losses < 0.02  # потери в трансформаторе видны на фоне погрешности


def test_meters_follow_topology():
    m = make(time_scale=60)
    m.write("F1.CB.CmdOpen", True)
    m.write("VL1.SimLoss", True)  # АВР переведёт 1 СШ на Т2
    run(m, 5)
    d = meter_deltas(m, 60)
    assert d["F1"] == (0.0, 0.0)
    assert d["T1"][0] == 0.0 and d["In1"][0] == 0.0
    assert d["F2"][0] > 0 and d["Aux1"][0] > 0  # 1 СШ питается через СВ
    assert d["In2"][0] > d["F2"][0] + d["F3"][0] + d["F4"][0]


def test_aux_moves_to_other_tsn_and_grows_in_frost():
    m = make(time_scale=60, start=datetime(2026, 1, 15, 3, 0))
    cold = meter_deltas(m, 60)["Aux2"][0]
    m2 = make(time_scale=60, start=datetime(2026, 7, 15, 13, 0))
    warm = meter_deltas(m2, 60)["Aux2"][0]
    assert cold > 1.5 * warm  # зимой обогрев

    m2.write("ABR.Enabled", False)
    m2.write("Sec1.InCB.CmdOpen", True)
    run(m2, 1)
    d = meter_deltas(m2, 60)
    assert d["Aux1"][0] == 0.0 and d["Aux2"][0] > 1.6 * warm  # вся нагрузка СН на ТСН-2


def test_day_run_accelerated_is_stable():
    m = make(time_scale=600, start=datetime(2026, 1, 15, 0, 0))  # сутки за 144 с модели
    start_a = m.meters["T1"].a + m.meters["T2"].a
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
    day_mwh = (m.meters["T1"].a + m.meters["T2"].a - start_a) / 1000
    assert 150 < day_mwh < 350  # приём ПС за сутки
