"""Modbus TCP сервер (asyncio, без сторонних библиотек).

Поддерживаются функции 1, 2, 3, 4, 5, 6, 15, 16. Номер устройства (Unit ID)
любой — ответ приходит с тем же номером.
"""

from __future__ import annotations

import asyncio
import logging
import struct

from . import registers as R
from .model import ACModel

log = logging.getLogger("ac_emulator")

EX_ILLEGAL_FUNCTION, EX_ILLEGAL_ADDRESS, EX_ILLEGAL_VALUE = 1, 2, 3
MAX_READ_BITS, MAX_READ_REGS, MAX_WRITE_BITS, MAX_WRITE_REGS = 2000, 125, 1968, 123


class ModbusError(Exception):
    def __init__(self, code: int):
        self.code = code


def _read_range(pdu: bytes, limit: int) -> tuple[int, int]:
    if len(pdu) != 5:
        raise ModbusError(EX_ILLEGAL_VALUE)
    addr, count = struct.unpack(">HH", pdu[1:5])
    if not 1 <= count <= limit:
        raise ModbusError(EX_ILLEGAL_VALUE)
    return addr, count


def _pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def handle_pdu(model: ACModel, pdu: bytes) -> bytes:
    """Обработать PDU запроса и вернуть PDU ответа (в т.ч. исключение)."""
    fc = pdu[0]
    try:
        try:
            if fc in (1, 2):
                addr, count = _read_range(pdu, MAX_READ_BITS)
                read = R.read_coil if fc == 1 else R.read_discrete
                data = _pack_bits([read(model, addr + i) for i in range(count)])
                return bytes([fc, len(data)]) + data
            if fc in (3, 4):
                addr, count = _read_range(pdu, MAX_READ_REGS)
                read = R.read_holding if fc == 3 else R.read_input
                values = [read(model, addr + i) for i in range(count)]
                return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *values)
            if fc == 5:
                if len(pdu) != 5:
                    raise ModbusError(EX_ILLEGAL_VALUE)
                addr, value = struct.unpack(">HH", pdu[1:5])
                if value not in (0x0000, 0xFF00):
                    raise ModbusError(EX_ILLEGAL_VALUE)
                R.write_coil(model, addr, value == 0xFF00)
                return pdu
            if fc == 6:
                if len(pdu) != 5:
                    raise ModbusError(EX_ILLEGAL_VALUE)
                addr, value = struct.unpack(">HH", pdu[1:5])
                R.write_holding(model, addr, value)
                return pdu
            if fc == 15:
                addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                if not 1 <= count <= MAX_WRITE_BITS or nbytes != (count + 7) // 8 or len(pdu) != 6 + nbytes:
                    raise ModbusError(EX_ILLEGAL_VALUE)
                for i in range(count):
                    R.read_coil(model, addr + i)  # проверить все адреса до записи
                for i in range(count):
                    R.write_coil(model, addr + i, bool(pdu[6 + i // 8] >> (i % 8) & 1))
                return pdu[:5]
            if fc == 16:
                addr, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                if not 1 <= count <= MAX_WRITE_REGS or nbytes != 2 * count or len(pdu) != 6 + nbytes:
                    raise ModbusError(EX_ILLEGAL_VALUE)
                for i in range(count):
                    R.read_holding(model, addr + i)
                for i, value in enumerate(struct.unpack(f">{count}H", pdu[6:6 + nbytes])):
                    R.write_holding(model, addr + i, value)
                return pdu[:5]
            raise ModbusError(EX_ILLEGAL_FUNCTION)
        except R.IllegalAddress:
            raise ModbusError(EX_ILLEGAL_ADDRESS) from None
        except R.IllegalValue as e:
            log.warning("Отклонена запись: %s", e)
            raise ModbusError(EX_ILLEGAL_VALUE) from None
        except struct.error:
            raise ModbusError(EX_ILLEGAL_VALUE) from None
    except ModbusError as e:
        return bytes([fc | 0x80, e.code])


class ACServer:
    def __init__(self, model: ACModel, host: str = "0.0.0.0", port: int = 502, tick: float = 0.1):
        self.model = model
        self.host, self.port, self.tick = host, port, tick
        self.ready = asyncio.Event()
        self.clients = 0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        name = f"{peer[0]}:{peer[1]}" if peer else "?"
        self.clients += 1
        log.info("Клиент подключился: %s", name)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0 or not 2 <= length <= 254:
                    break  # не Modbus TCP — закрываем соединение
                pdu = await reader.readexactly(length - 1)
                resp = handle_pdu(self.model, pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.clients -= 1
            log.info("Клиент отключился: %s", name)
            writer.close()

    async def run(self) -> None:
        server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("Modbus TCP сервер слушает %s:%d (Unit ID любой)", self.host, self.port)
        self.ready.set()
        loop = asyncio.get_running_loop()
        last = loop.time()
        async with server:
            while True:
                await asyncio.sleep(self.tick)
                now = loop.time()
                self.model.step(now - last)
                last = now
