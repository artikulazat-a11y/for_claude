"""Эмулятор кондиционеров: по Modbus TCP серверу на каждый кондиционер, порты подряд (601, 602, …)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

from . import modbus
from .model import SEV_ALARM, SEV_WARNING, AirConditioner, Fleet
from .modbus import ILLEGAL_DATA_ADDRESS, ModbusError, ModbusTcpServer
from .registers import COILS, DISCRETE_INPUTS, REGISTER_AT, REGISTER_COUNT, encode_registers

log = logging.getLogger("ac")

LOG_LEVEL = {SEV_ALARM: logging.ERROR, SEV_WARNING: logging.WARNING}


class ACRegisters:
    """Регистры Modbus одного кондиционера поверх модели."""

    def __init__(self, unit: AirConditioner, on_write: Callable[[], None] = lambda: None):
        self.unit = unit
        self.on_write = on_write  # записать события в журнал сразу, а не на следующем шаге модели

    def read_registers(self, address: int, count: int) -> list[int]:
        if address + count > REGISTER_COUNT:
            raise ModbusError(ILLEGAL_DATA_ADDRESS)
        return encode_registers(self.unit.values)[address:address + count]

    def write_registers(self, address: int, values: list[int]) -> None:
        if address + len(values) > REGISTER_COUNT:
            raise ModbusError(ILLEGAL_DATA_ADDRESS)
        # Сначала проверяем всё, потом пишем: запрос выполняется целиком или не выполняется
        writes = []
        for i, raw in enumerate(values):
            reg = REGISTER_AT.get(address + i, (None, 0))[0]
            if reg is None or not reg.writable:
                raise ModbusError(ILLEGAL_DATA_ADDRESS)
            value, err = self.unit.check(reg.name, reg.decode(raw))
            if err:
                raise ModbusError(getattr(modbus, err))
            writes.append((reg.name, value))
        for name, value in writes:
            self.unit.write(name, value)
        self.on_write()

    def read_coils(self, address: int, count: int) -> list[bool]:
        return self._bits(COILS, address, count)

    def write_coils(self, address: int, values: list[bool]) -> None:
        if address + len(values) > len(COILS):
            raise ModbusError(ILLEGAL_DATA_ADDRESS)
        for bit, value in zip(COILS[address:], values):
            self.unit.write(bit.name, int(value))
        self.on_write()

    def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        return self._bits(DISCRETE_INPUTS, address, count)

    def _bits(self, table: list, address: int, count: int) -> list[bool]:
        if address + count > len(table):
            raise ModbusError(ILLEGAL_DATA_ADDRESS)
        return [bool(self.unit.values[b.name]) for b in table[address:address + count]]


class ACServer:
    def __init__(self, fleet: Fleet, host: str = "0.0.0.0", base_port: int = 601, tick: float = 0.2,
                 state_file: str | None = "ac_state.json", state_save_period: float = 10.0):
        self.fleet = fleet
        self.host = host
        self.base_port = base_port
        self.tick = tick
        # В файле хранятся уставки, аварии, счётчики энергии и наработки, расписание случайных аварий
        self.state_file = Path(state_file) if state_file else None
        self.state_save_period = state_save_period
        self._last_save = time.monotonic()
        self._save_lock = threading.Lock()
        self._state_loaded = False  # не сохранять, пока не прочитан файл, — иначе затрём счётчики
        self.servers: dict[int, ModbusTcpServer] = {}
        self.ready = asyncio.Event()

    def port(self, number: int) -> int:
        return self.base_port + number - 1

    def name(self, number: int) -> str:
        return f"{self.fleet.units[number].title} (порт {self.port(number)})"

    async def start(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                self.fleet.load_state(json.loads(self.state_file.read_text(encoding="utf-8")))
                log.info("Состояние кондиционеров загружено из %s", self.state_file)
            except (OSError, ValueError, AttributeError) as e:
                log.warning("Не удалось прочитать %s: %s — используются уставки по умолчанию", self.state_file, e)
        self._state_loaded = True

        errors: list[OSError] = []
        for n, unit in self.fleet.units.items():
            srv = ModbusTcpServer(ACRegisters(unit, self._log_events), self.host, self.port(n), self.name(n))
            try:
                await srv.start()
            except OSError as e:
                errors.append(e)
                log.error("%s: не удалось открыть порт: %s", self.name(n), e)
                continue
            self.servers[n] = srv
        if not self.servers:
            raise errors[0]
        ports = sorted(s.port for s in self.servers.values())
        log.info("Modbus TCP: %d кондиционеров на %s, порты %d…%d", len(self.servers), self.host, ports[0], ports[-1])

    async def stop(self) -> None:
        for srv in self.servers.values():
            await srv.close()
        self.servers.clear()

    def save_state(self, force: bool = False) -> None:
        """Сохранить уставки и аварии (сразу после изменения) и счётчики (раз в state_save_period).
        Может вызываться из другого потока — при закрытии окна консоли на Windows."""
        if not (self.state_file and self._state_loaded):
            return
        due = time.monotonic() - self._last_save >= self.state_save_period
        if not (force or due or self.fleet.settings_dirty):
            return
        with self._save_lock:
            for u in self.fleet.units.values():
                u.dirty = False
            self._last_save = time.monotonic()
            tmp = self.state_file.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.fleet.state(), ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self.state_file)
            except OSError as e:
                log.warning("Не удалось сохранить состояние в %s: %s", self.state_file, e)

    def _log_events(self) -> None:
        for number, severity, text in self.fleet.pop_events():
            log.log(LOG_LEVEL.get(severity, logging.INFO), "%s: %s", self.name(number), text)

    async def run(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        try:
            self.ready.set()
            last = loop.time()
            while True:
                now = loop.time()
                self.fleet.step(min(now - last, 1.0))
                last = now
                self._log_events()
                self.save_state()
                await asyncio.sleep(max(0.0, self.tick - (loop.time() - now)))
        finally:
            await self.stop()
