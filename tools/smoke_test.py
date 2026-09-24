"""Проверка запущенного эмулятора: чтение измерений и команда на отключение/включение фидера.

python tools/smoke_test.py [opc.tcp://127.0.0.1:4840/substation/]
"""

import asyncio
import sys

from asyncua import Client, ua


async def wait_state(node, expected, timeout=5.0):
    for _ in range(int(timeout / 0.1)):
        if await node.read_value() == expected:
            return True
        await asyncio.sleep(0.1)
    return False


async def main(url: str) -> int:
    for attempt in range(30):  # ждём, пока сервер поднимется
        try:
            client = Client(url)
            await client.connect()
            break
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(1)
    else:
        print(f"FAIL: нет связи с {url}")
        return 1
    try:
        ns = await client.get_namespace_index("urn:substation-emulator")

        def node(path):
            return client.get_node(ua.NodeId(path, ns))

        u = await node("Sec1.U").read_value()
        i = await node("F1.I").read_value()
        print(f"Sec1.U = {u} кВ, F1.I = {i} А")
        if not (9.5 < u < 11.5 and i > 0):
            print("FAIL: измерения вне ожидаемого диапазона")
            return 1
        await node("F1.CB.CmdOpen").write_value(True)
        if not await wait_state(node("F1.CB.State"), 1):
            print("FAIL: фидер не отключился")
            return 1
        await node("F1.CB.CmdClose").write_value(True)
        if not await wait_state(node("F1.CB.State"), 2):
            print("FAIL: фидер не включился")
            return 1
        print("OK: чтение и команды работают")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "opc.tcp://127.0.0.1:4840/substation/")))
