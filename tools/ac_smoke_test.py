"""Проверка запущенного эмулятора кондиционеров без сторонних библиотек:
чтение регистров первого и последнего кондиционера, выключение и включение, имитация и сброс аварии.

python tools/ac_smoke_test.py [host] [base_port] [count]    (по умолчанию 127.0.0.1 601 50)
"""

import socket
import struct
import sys
import time

SETPOINT, SIM_ALARM, ALARM_RESET, POWER = 2, 5, 4, 0
STATE, ALARM_CODE, DEVICE_NUMBER = 10, 19, 27


class Client:
    def __init__(self, host: str, port: int):
        for _ in range(30):  # ждём, пока сервер поднимется
            try:
                self.sock = socket.create_connection((host, port), timeout=3)
                break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit(f"FAIL: нет связи с {host}:{port}")
        self.tid = 0

    def request(self, pdu: bytes) -> bytes:
        self.tid += 1
        self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, 1) + pdu)
        head = self._recv(7)
        tid, _proto, length, _unit = struct.unpack(">HHHB", head)
        resp = self._recv(length - 1)
        if tid != self.tid or resp[0] & 0x80:
            raise RuntimeError(f"ошибка Modbus: {resp.hex()}")
        return resp

    def _recv(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("соединение закрыто")
            data += chunk
        return data

    def read(self, address: int, count: int = 1) -> list[int]:
        resp = self.request(struct.pack(">BHH", 3, address, count))
        return list(struct.unpack(f">{count}H", resp[2:]))

    def write(self, address: int, value: int) -> None:
        self.request(struct.pack(">BHH", 6, address, value))

    def wait(self, address: int, expected: int, timeout: float = 5.0) -> bool:
        for _ in range(int(timeout / 0.1)):
            if self.read(address)[0] == expected:
                return True
            time.sleep(0.1)
        return False


def main(host: str, base: int, count: int) -> int:
    for n in (1, count):
        c = Client(host, base + n - 1)
        regs = c.read(0, 29)
        room, number = regs[12] - (0x10000 if regs[12] >= 0x8000 else 0), regs[DEVICE_NUMBER]
        print(f"Порт {base + n - 1}: кондиционер №{number}, t = {room / 10} °C, состояние {regs[STATE]}")
        if number != n or not 100 < room < 400:
            print("FAIL: неверный номер кондиционера или температура")
            return 1
    c.write(POWER, 0)
    if not c.wait(STATE, 0):
        print("FAIL: кондиционер не выключился")
        return 1
    c.write(POWER, 1)
    c.write(SIM_ALARM, 3)
    if not c.wait(STATE, 5) or c.read(ALARM_CODE)[0] != 3:
        print("FAIL: авария не появилась")
        return 1
    c.write(ALARM_RESET, 1)
    if not c.wait(ALARM_CODE, 0):
        print("FAIL: авария не сбросилась")
        return 1
    print("OK: чтение, команды и сброс аварии работают")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(main(a[0] if a else "127.0.0.1", int(a[1]) if len(a) > 1 else 601, int(a[2]) if len(a) > 2 else 50))
