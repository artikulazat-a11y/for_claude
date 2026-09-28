"""Эмулятор кондиционера: модель и настоящий Modbus TCP клиент (pymodbus) против сервера."""

import asyncio
from datetime import datetime, timedelta

import pytest
from pymodbus.client import AsyncModbusTcpClient

from ac_emulator.model import (
    FAULT_MIN_INTERVAL, MODE_COOL, STATE_COOLING, STATE_FAULT, STATE_OFF, ACModel,
)
from ac_emulator.server import ACServer

START = datetime(2026, 9, 28, 12, 0)


def test_cooling_reaches_setpoint_smoothly():
    m = ACModel(seed=1, start=START, faults_enabled=False, room_temp=27.0)
    m.set_power(True)
    m.step(1)
    assert m.fan_percent <= 5.0  # вентилятор разгоняется плавно, не рывком
    m.step(120)
    assert m.state == STATE_COOLING and m.fan_percent == 100.0 and m.supply_temp < m.room_temp
    m.step(1800)
    assert abs(m.room_temp - 22.0) < 1.0
    assert 0 < m.fan_percent < 100.0  # у уставки инвертор сбрасывает обороты


def test_power_off_stops_fan_smoothly():
    m = ACModel(seed=1, start=START, faults_enabled=False)
    m.set_power(True)
    m.step(60)
    m.set_power(False)
    m.step(1)
    assert m.fan_percent > 0
    m.step(60)
    assert m.fan_percent == 0 and m.compressor_percent == 0 and m.state == STATE_OFF


def test_setpoint_range_checked():
    m = ACModel(seed=1, start=START)
    with pytest.raises(ValueError):
        m.set_setpoint(35.0)


def test_random_fault_at_most_once_per_day_and_reset():
    m = ACModel(seed=7, start=START)
    m.set_power(True)
    m.set_mode(MODE_COOL)
    faults = []
    for _ in range(30 * 24 * 60):  # 30 суток шагами по минуте
        m.step(60)
        if m.fault_code:
            assert m.state == STATE_FAULT and not m.running
            faults.append(m.now)
            m.step(60)
            assert m.fan_percent < 100 and m.compressor_percent == 0  # агрегат остановлен
            assert m.reset_fault()
            assert m.running
    assert len(faults) >= 5
    assert min(b - a for a, b in zip(faults, faults[1:])) >= timedelta(seconds=FAULT_MIN_INTERVAL)


@pytest.fixture
async def client():
    model = ACModel(seed=1, start=START, faults_enabled=False, time_scale=60)
    # Порт выбирает ОС: на Windows фиксированный порт может попасть в зарезервированный диапазон
    srv = ACServer(model, host="127.0.0.1", port=0, tick=0.05)
    task = asyncio.create_task(srv.run())
    ready = asyncio.create_task(srv.ready.wait())
    await asyncio.wait({task, ready}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()  # сервер не запустился — показать настоящую ошибку
    assert srv.ready.is_set()
    c = AsyncModbusTcpClient("127.0.0.1", port=srv.port)
    await c.connect()
    yield c
    c.close()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_modbus_client_controls_unit(client):
    rr = await client.read_holding_registers(0, count=6)
    assert rr.registers == [0, 220, 0, 0, 0, 0]
    ir = await client.read_input_registers(0, count=11)
    assert ir.registers[6] == STATE_OFF and ir.registers[3] == 0

    assert not (await client.write_coil(0, True)).isError()
    assert not (await client.write_register(1, 200)).isError()  # уставка 20.0 °C
    await asyncio.sleep(1.0)  # 60 модельных секунд
    ir = await client.read_input_registers(0, count=11)
    assert ir.registers[6] == STATE_COOLING and ir.registers[3] > 0 and ir.registers[9] > 0
    di = await client.read_discrete_inputs(0, count=6)
    assert di.bits[:6] == [True, False, True, True, True, False]
    assert (await client.read_coils(0, count=2)).bits[:2] == [True, False]


async def test_modbus_fault_and_reset(client):
    await client.write_register(0, 1)
    assert not (await client.write_register(5, 2)).isError()  # тестовая авария «высокое давление»
    ir = await client.read_input_registers(6, count=5)
    assert ir.registers[0] == STATE_FAULT and ir.registers[1] == 2 and ir.registers[4] == 1
    assert (await client.read_discrete_inputs(1, count=1)).bits[0]

    assert not (await client.write_coil(1, True)).isError()  # сброс аварии
    ir = await client.read_input_registers(7, count=1)
    assert ir.registers[0] == 0
    await client.write_register(5, 4)
    await client.write_registers(4, [1])  # сброс через holding-регистр
    assert (await client.read_input_registers(7, count=1)).registers[0] == 0


async def test_modbus_errors(client):
    rr = await client.write_register(1, 400)  # уставка 40 °C вне диапазона
    assert rr.isError() and rr.exception_code == 3
    rr = await client.read_holding_registers(100, count=1)
    assert rr.isError() and rr.exception_code == 2
    rr = await client.read_input_registers(8, count=10)  # выходит за конец карты
    assert rr.isError() and rr.exception_code == 2
