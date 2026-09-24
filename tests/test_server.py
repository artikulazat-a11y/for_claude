"""Интеграционные тесты: настоящий OPC UA клиент против запущенного эмулятора."""

import asyncio
import json
from datetime import datetime

import pytest
from asyncua import Client, ua
from asyncua.ua.uaerrors import BadNotWritable, BadOutOfRange

from substation_emulator.model import Model
from substation_emulator.server import SubstationServer
from substation_emulator.tags import CLOSED, OPEN, TAGS

PORT = 48411


@pytest.fixture
async def emulator(tmp_path):
    model = Model(seed=1, start=datetime(2026, 9, 24, 19, 0))
    srv = SubstationServer(model, host="127.0.0.1", port=PORT, tick=0.05,
                           setpoints_file=str(tmp_path / "setpoints.json"))
    task = asyncio.create_task(srv.run())
    await asyncio.wait_for(srv.ready.wait(), 10)
    async with Client(f"opc.tcp://127.0.0.1:{PORT}/substation/") as client:
        yield srv, client
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def node(client, path):
    ns = await client.get_namespace_index("urn:substation-emulator")
    return client.get_node(ua.NodeId(path, ns))


async def wait_value(n, expected, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        v = await n.read_value()
        if v == expected:
            return v
        await asyncio.sleep(0.05)
    return await n.read_value()


async def test_all_tags_readable(emulator):
    _srv, client = emulator
    for tag in TAGS:
        await (await node(client, tag.path)).read_value()
    assert await (await node(client, "Sec1.U")).read_value() > 9.5
    assert 49.9 < await (await node(client, "Station.Frequency")).read_value() < 50.1


async def test_breaker_command_open_close(emulator):
    _srv, client = emulator
    state = await node(client, "F1.CB.State")
    current = await node(client, "F1.I")
    cmd_open = await node(client, "F1.CB.CmdOpen")
    assert await state.read_value() == CLOSED
    assert await current.read_value() > 0

    await cmd_open.write_value(True)
    assert await wait_value(state, OPEN) == OPEN
    assert await wait_value(current, 0.0) == 0.0
    assert await cmd_open.read_value() is False  # команда — импульс, сброшена эмулятором

    await (await node(client, "F1.CB.CmdClose")).write_value(True)
    assert await wait_value(state, CLOSED) == CLOSED


async def test_setpoint_range_and_readonly(emulator):
    srv, client = emulator
    uset = await node(client, "T1.AVR.Uset")
    await uset.write_value(10.8)
    assert await uset.read_value() == 10.8
    assert srv.model.trafos[1].uset == 10.8
    await asyncio.sleep(0.2)
    assert json.loads(srv.setpoints_file.read_text(encoding="utf-8"))["T1.AVR.Uset"] == 10.8

    with pytest.raises(BadOutOfRange):
        await uset.write_value(20.0)
    assert await uset.read_value() == 10.8

    with pytest.raises(BadNotWritable):
        await (await node(client, "T1.OilTemp")).write_value(20.0)


async def test_int_written_to_double_setpoint(emulator):
    srv, client = emulator
    iset = await node(client, "F2.Prot.Iset")
    await iset.write_value(ua.DataValue(ua.Variant(700, ua.VariantType.Int32)))
    assert await iset.read_value() == 700.0
    assert srv.model.feeders["F2"].iset == 700.0


class _Events:
    def __init__(self):
        self.messages = []

    def event_notification(self, event):
        self.messages.append((event.Severity, event.Message.Text))


async def test_fault_trips_feeder(emulator):
    _srv, client = emulator
    handler = _Events()
    sub = await client.create_subscription(50, handler)
    await sub.subscribe_events(client.nodes.server)
    await (await node(client, "F3.SimFault")).write_value(True)
    assert await wait_value(await node(client, "F3.Prot.Trip"), True) is True
    assert await wait_value(await node(client, "F3.CB.State"), OPEN) == OPEN
    assert await (await node(client, "Station.GeneralAlarm")).read_value() is True
    await asyncio.sleep(0.3)
    assert any(sev >= 800 and "МТЗ" in text for sev, text in handler.messages), handler.messages


def test_benign_session_faults_hidden_real_errors_kept():
    import logging

    from substation_emulator.server import _QuietSessionFaults

    f = _QuietSessionFaults()

    def rec(msg, *args):
        return logging.LogRecord("asyncua.server.uaprocessor", logging.ERROR, __file__, 0, msg, args, None)

    fault = "sending service fault response: %s (%s)"
    assert not f.filter(rec(fault, "The session cannot be used...", "BadSessionNotActivated"))
    assert not f.filter(rec(fault, "The session id is not valid.", "BadSessionIdInvalid"))
    assert f.filter(rec(fault, "An internal error occurred.", "BadInternalError"))
    assert f.filter(rec("Error while processing message"))
