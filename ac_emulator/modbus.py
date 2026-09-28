"""Modbus TCP сервер на asyncio без внешних библиотек.

Функции: 1 (Read Coils), 2 (Read Discrete Inputs), 3 (Read Holding Registers),
4 (Read Input Registers), 5 (Write Single Coil), 6 (Write Single Register),
15 (Write Multiple Coils), 16 (Write Multiple Registers). Unit ID в запросе
не проверяется: на каждом порту одно устройство, ответ приходит с тем же Unit ID.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Protocol

log = logging.getLogger("ac")

# Коды исключений Modbus
ILLEGAL_FUNCTION = 1
ILLEGAL_DATA_ADDRESS = 2
ILLEGAL_DATA_VALUE = 3
SERVER_DEVICE_FAILURE = 4


class ModbusError(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class DataStore(Protocol):
    """Данные одного устройства. Методы бросают ModbusError при ошибке адреса или значения."""

    def read_registers(self, address: int, count: int) -> list[int]: ...

    def write_registers(self, address: int, values: list[int]) -> None: ...

    def read_coils(self, address: int, count: int) -> list[bool]: ...

    def write_coils(self, address: int, values: list[bool]) -> None: ...

    def read_discrete_inputs(self, address: int, count: int) -> list[bool]: ...


def _pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _unpack_bits(data: bytes, count: int) -> list[bool]:
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


def process_pdu(store: DataStore, pdu: bytes) -> bytes:
    """Обработка PDU запроса, возвращает PDU ответа (или исключения)."""
    fc = pdu[0] if pdu else 0
    try:
        return _dispatch(store, fc, pdu)
    except ModbusError as e:
        return bytes([(fc | 0x80) & 0xFF, e.code])
    except struct.error:  # запрос короче, чем положено для функции
        return bytes([(fc | 0x80) & 0xFF, ILLEGAL_DATA_VALUE])
    except Exception:
        log.exception("Ошибка обработки запроса Modbus")
        return bytes([(fc | 0x80) & 0xFF, SERVER_DEVICE_FAILURE])


def _dispatch(store: DataStore, fc: int, pdu: bytes) -> bytes:
    if fc in (1, 2):
        address, count = struct.unpack_from(">HH", pdu, 1)
        if not 1 <= count <= 2000:
            raise ModbusError(ILLEGAL_DATA_VALUE)
        bits = store.read_coils(address, count) if fc == 1 else store.read_discrete_inputs(address, count)
        data = _pack_bits(bits)
        return bytes([fc, len(data)]) + data
    if fc in (3, 4):
        address, count = struct.unpack_from(">HH", pdu, 1)
        if not 1 <= count <= 125:
            raise ModbusError(ILLEGAL_DATA_VALUE)
        regs = store.read_registers(address, count)
        return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *regs)
    if fc == 5:
        address, value = struct.unpack_from(">HH", pdu, 1)
        if value not in (0x0000, 0xFF00):
            raise ModbusError(ILLEGAL_DATA_VALUE)
        store.write_coils(address, [value == 0xFF00])
        return pdu[:5]
    if fc == 6:
        address, value = struct.unpack_from(">HH", pdu, 1)
        store.write_registers(address, [value])
        return pdu[:5]
    if fc == 15:
        address, count, nbytes = struct.unpack_from(">HHB", pdu, 1)
        if not 1 <= count <= 1968 or nbytes != (count + 7) // 8 or len(pdu) < 6 + nbytes:
            raise ModbusError(ILLEGAL_DATA_VALUE)
        store.write_coils(address, _unpack_bits(pdu[6:6 + nbytes], count))
        return pdu[:5]
    if fc == 16:
        address, count, nbytes = struct.unpack_from(">HHB", pdu, 1)
        if not 1 <= count <= 123 or nbytes != 2 * count or len(pdu) < 6 + nbytes:
            raise ModbusError(ILLEGAL_DATA_VALUE)
        store.write_registers(address, list(struct.unpack_from(f">{count}H", pdu, 6)))
        return pdu[:5]
    raise ModbusError(ILLEGAL_FUNCTION)


def _peer(name: object) -> str:
    if isinstance(name, tuple) and len(name) >= 2:
        return f"{name[0]}:{name[1]}"
    return str(name)


class ModbusTcpServer:
    """Modbus TCP сервер одного устройства на своём порту."""

    def __init__(self, store: DataStore, host: str, port: int, name: str):
        self.store, self.host, self.port, self.name = store, host, port, name
        self.clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.port)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for w in list(self.clients):
            w.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), 2)
        except (asyncio.TimeoutError, OSError):
            pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = _peer(writer.get_extra_info("peername"))
        self.clients.add(writer)
        log.info("%s: клиент подключился %s", self.name, peer)
        try:
            while True:
                tid, proto, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                if proto != 0 or not 2 <= length <= 254:
                    log.warning("%s: неверный заголовок Modbus TCP от %s, соединение закрыто", self.name, peer)
                    break
                pdu = await reader.readexactly(length - 1)
                resp = process_pdu(self.store, pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()
            log.info("%s: клиент отключился %s", self.name, peer)
