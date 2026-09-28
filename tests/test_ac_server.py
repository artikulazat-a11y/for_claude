"""Интеграционные тесты: сторонний Modbus TCP клиент (pymodbus) против запущенного эмулятора кондиционеров."""

import asyncio
import json
import struct
from datetime import datetime

import pytest
from pymodbus.client import AsyncModbusTcpClient

from ac_emulator.model import Fleet
from ac_emulator.modbus import process_pdu
from ac_emulator.registers import REGISTER_COUNT, REGISTERS_BY_NAME, ST_ALARM, ST_OFF
from ac_emulator.server import ACRegisters, ACServer

BASE = 15601
COUNT = 3


def addr(name):
    return REGISTERS_BY_NAME[name].address


@pytest.fixture
async def emulator(tmp_path):
    fleet = Fleet(count=COUNT, seed=1, start=datetime(2026, 7, 15, 14, 0), random_alarms=False)
    srv = ACServer(fleet, host="127.0.0.1", base_port=BASE, tick=0.05, state_file=str(tmp_path / "ac_state.json"))
    task = asyncio.create_task(srv.run())
    await asyncio.wait_for(srv.ready.wait(), 10)
    clients = {}
    for n in range(1, COUNT + 1):
        c = AsyncModbusTcpClient("127.0.0.1", port=BASE + n - 1)
        assert await c.connect()
        clients[n] = c
    yield srv, clients
    for c in clients.values():
        c.close()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def read(client, name, count=1):
    r = await client.read_holding_registers(addr(name), count=count)
    assert not r.isError(), r
    return r.registers if count > 1 else r.registers[0]


async def wait_reg(client, name, expected, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if await read(client, name) == expected:
            return expected
        await asyncio.sleep(0.05)
    return await read(client, name)


async def test_each_port_is_its_own_unit(emulator):
    _srv, clients = emulator
    for n, c in clients.items():
        assert await read(c, "DeviceNumber") == n
        hr = await c.read_holding_registers(0, count=REGISTER_COUNT)
        ir = await c.read_input_registers(0, count=REGISTER_COUNT)
        assert len(hr.registers) == REGISTER_COUNT
        # Функции 3 и 4 читают одну таблицу (измерения между запросами могли смениться)
        same = [*range(10), addr("DeviceNumber"), addr("Capacity")]
        assert [hr.registers[i] for i in same] == [ir.registers[i] for i in same]
        assert 150 < await read(c, "RoomTemp") < 300  # 15…30 °C


async def test_setpoint_write_and_exceptions(emulator):
    srv, clients = emulator
    c = clients[2]
    assert not (await c.write_register(addr("Setpoint"), 185)).isError()
    assert await read(c, "Setpoint") == 185
    assert srv.fleet.units[2].setpoint == 18.5
    assert srv.fleet.units[1].setpoint == 22.0  # другие кондиционеры не затронуты
    await asyncio.sleep(0.2)
    saved = json.loads(srv.state_file.read_text(encoding="utf-8"))
    assert saved["2"]["Setpoint"] == 18.5

    r = await c.write_register(addr("Setpoint"), 350)  # 35 °C — вне диапазона 16…30
    assert r.isError() and r.exception_code == 3
    r = await c.write_register(addr("RoomTemp"), 200)  # только чтение
    assert r.isError() and r.exception_code == 2
    r = await c.read_holding_registers(REGISTER_COUNT - 2, count=5)
    assert r.isError() and r.exception_code == 2
    assert await read(c, "Setpoint") == 185


async def test_multiple_write_is_atomic(emulator):
    srv, clients = emulator
    c = clients[1]
    r = await c.write_registers(0, [0, 9, 200])  # режим 9 не существует — не записывается ничего
    assert r.isError() and r.exception_code == 3
    assert srv.fleet.units[1].power
    assert not (await c.write_registers(0, [1, 1, 200, 3])).isError()
    assert await read(c, "Power", 4) == [1, 1, 200, 3]


async def test_power_alarm_and_reset_via_coils(emulator):
    srv, clients = emulator
    c = clients[3]
    assert not (await c.write_coil(0, False)).isError()
    assert await wait_reg(c, "State", ST_OFF) == ST_OFF
    di = await c.read_discrete_inputs(0, count=4)
    assert di.bits[:4] == [False, False, False, False]
    await c.write_coil(0, True)

    await c.write_register(addr("SimAlarm"), 2)
    assert await read(c, "AlarmCode") == 2
    assert await read(c, "State") == ST_ALARM
    assert await read(c, "SimAlarm") == 0  # команда не «залипает»
    assert (await c.read_discrete_inputs(2, count=1)).bits[0] is True

    await c.write_coil(1, True)  # сброс аварии
    assert await read(c, "AlarmCode") == 0
    assert await read(c, "LastAlarmCode") == 2
    assert (await c.read_coils(0, count=2)).bits[:2] == [True, False]
    assert srv.fleet.units[3].alarm_count >= 1


async def test_raw_frames_any_unit_id_and_bad_function(emulator):
    _srv, _clients = emulator
    reader, writer = await asyncio.open_connection("127.0.0.1", BASE)
    try:
        async def request(pdu, unit=255, tid=7):
            writer.write(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
            await writer.drain()
            head = await reader.readexactly(7)
            r_tid, proto, length, r_unit = struct.unpack(">HHHB", head)
            assert (r_tid, proto, r_unit) == (tid, 0, unit)
            return await reader.readexactly(length - 1)

        resp = await request(struct.pack(">BHH", 3, addr("DeviceNumber"), 1))
        assert resp == bytes([3, 2, 0, 1])
        assert await request(bytes([0x2B, 0x0E, 1, 0])) == bytes([0xAB, 1])  # функция не поддерживается
        assert await request(bytes([3, 0])) == bytes([0x83, 3])  # короткий запрос
    finally:
        writer.close()


def test_process_pdu_bits_and_limits():
    fleet = Fleet(count=1, seed=1, random_alarms=False)
    store = ACRegisters(fleet.units[1])
    # Катушки: Power=1, AlarmReset=0 -> 0b01
    assert process_pdu(store, struct.pack(">BHH", 1, 0, 2)) == bytes([1, 1, 0b01])
    assert process_pdu(store, struct.pack(">BHH", 1, 0, 3)) == bytes([0x81, 2])
    assert process_pdu(store, struct.pack(">BHH", 3, 0, 0)) == bytes([0x83, 3])
    assert process_pdu(store, struct.pack(">BHH", 3, 0, 126)) == bytes([0x83, 3])
    assert process_pdu(store, struct.pack(">BHH", 5, 0, 0x1234)) == bytes([0x85, 3])
    # Запись нескольких катушек: выключить
    assert process_pdu(store, struct.pack(">BHHBB", 15, 0, 1, 1, 0)) == struct.pack(">BHH", 15, 0, 1)
    assert not fleet.units[1].power
