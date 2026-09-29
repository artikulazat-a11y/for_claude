"""OPC UA эмулятор приточно-вытяжной установки ПВУ-1.

Запуск:  python pvu_emulator.py            (порт 4891, аварии раз в ~3 минуты)
Справка: python pvu_emulator.py --help
"""

import argparse
import asyncio
import logging
import random
import sys
import time
from datetime import datetime, timezone

from asyncua import Server, ua, uamethod

from pvu_faults import SCENARIOS, FaultInjector
from pvu_model import PVUModel
from pvu_tags import COMMAND_TAGS, FOLDERS, TAGS

NS_URI = "urn:pvu1:emulator"
TICK = 0.2            # период цикла, с реального времени
MAX_SUBSTEP = 0.5     # шаг интегрирования модели, с модельного времени
VTYPE = {
    "Double": ua.VariantType.Double,
    "Boolean": ua.VariantType.Boolean,
    "String": ua.VariantType.String,
    "Int32": ua.VariantType.Int32,
    "UInt32": ua.VariantType.UInt32,
}
DEFAULT = {"Double": 0.0, "Boolean": False, "String": "", "Int32": 0, "UInt32": 0}
LEVEL = {"info": ("ИНФО  ", 200), "ok": ("НОРМА ", 300), "warn": ("ВНИМ. ", 500), "alarm": ("АВАРИЯ", 800)}


def parse_args():
    p = argparse.ArgumentParser(description="OPC UA эмулятор ПВУ-1")
    p.add_argument("--host", default="0.0.0.0", help="адрес для прослушивания (по умолчанию все интерфейсы)")
    p.add_argument("--port", type=int, default=4891, help="порт OPC UA (по умолчанию 4891)")
    p.add_argument("--speed", type=float, default=1.0, help="ускорение времени модели, например 5")
    p.add_argument("--fault-interval", type=float, default=180.0, help="среднее время между неисправностями, с модели")
    p.add_argument("--no-faults", action="store_true", help="не имитировать случайные неисправности")
    p.add_argument("--fault", choices=sorted(SCENARIOS), help="сразу включить эту неисправность")
    p.add_argument("--auto-reset", type=float, default=60.0, help="автосброс блокировки через N с после срабатывания, если причина ушла; 0 — только оператором")
    p.add_argument("--no-autostart", action="store_true", help="не запускать установку при старте эмулятора")
    p.add_argument("--seed", type=int, help="зерно генератора случайных чисел для повторяемых сценариев")
    return p.parse_args()


class Emulator:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed)
        self.pending_events = []
        self.last_event = ""
        self.model = PVUModel(self.rng, self.log, auto_reset=args.auto_reset)
        self.faults = FaultInjector(self.model, self.rng, self.log, interval=args.fault_interval, enabled=not args.no_faults)
        self.nodes = {}
        self.vtypes = {}
        self.written = {}
        self.heartbeat = 0

    # ---------------- журнал ----------------
    def log(self, level, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"{stamp}  {LEVEL[level][0]}  {text}", flush=True)
        self.last_event = f"{stamp} {text}"
        self.pending_events.append((level, text))

    # ---------------- адресное пространство ----------------
    async def build(self):
        a = self.args
        self.server = server = Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://{a.host}:{a.port}/")
        server.set_server_name("PVU-1 emulator")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.idx = idx = await server.register_namespace(NS_URI)

        root = await server.nodes.objects.add_object(ua.NodeId("PVU1", idx), ua.QualifiedName("PVU1", idx))
        await self._describe(root, "Приточно-вытяжная установка ПВУ-1")
        folders = {}
        for key, title in FOLDERS.items():
            folders[key] = await root.add_folder(ua.NodeId(f"PVU1.{key}", idx), ua.QualifiedName(key, idx))
            await self._describe(folders[key], title)

        for name, folder, typ, unit, desc, writable in TAGS:
            node = await folders[folder].add_variable(
                ua.NodeId(f"PVU1.{name}", idx), ua.QualifiedName(name, idx), ua.Variant(DEFAULT[typ], VTYPE[typ]))
            await self._describe(node, desc)
            if unit:
                await node.add_property(ua.NodeId(f"PVU1.{name}.Unit", idx), ua.QualifiedName("Unit", idx),
                                        ua.Variant(unit, ua.VariantType.String))
            if writable:
                await node.set_writable()
            self.nodes[name] = node
            self.vtypes[name] = VTYPE[typ]
            self.written[name] = DEFAULT[typ]

        # методы — для клиентов, которые умеют их вызывать
        m, fi = self.model, self.faults

        @uamethod
        def start(parent):
            return m.command("start")

        @uamethod
        def stop(parent):
            return m.command("stop")

        @uamethod
        def reset(parent):
            return m.command("reset")

        @uamethod
        def set_setpoint(parent, value):
            return m.command("setpoint", value)

        @uamethod
        def replace_filter(parent):
            return m.command("replace_filter")

        @uamethod
        def inject_fault(parent, name):
            if name in ("", "clear", "none"):
                fi.clear()
                return True
            if name not in SCENARIOS:
                self.log("warn", f"InjectFault: нет неисправности «{name}»")
                return False
            fi.inject(name)
            return True

        B, D, S = ua.VariantType.Boolean, ua.VariantType.Double, ua.VariantType.String
        for mname, func, inputs in (
            ("Start", start, []), ("Stop", stop, []), ("ResetAlarms", reset, []),
            ("SetSetpoint", set_setpoint, [D]), ("ReplaceFilter", replace_filter, []),
            ("InjectFault", inject_fault, [S]),
        ):
            await root.add_method(ua.NodeId(f"PVU1.{mname}", idx), ua.QualifiedName(mname, idx), func, inputs, [B])

        self.evgen = await server.get_event_generator()

    async def _describe(self, node, text):
        await node.write_attribute(ua.AttributeIds.Description, ua.DataValue(ua.Variant(ua.LocalizedText(text, "ru-RU"))))

    # ---------------- обмен с клиентами ----------------
    async def read_client_writes(self):
        """Сравниваем записываемые теги с тем, что писали сами: разница — запись клиента."""
        m = self.model
        for name in (*COMMAND_TAGS, "room_sp", "key_sa_local", "key_ea_local"):
            val = await self.nodes[name].read_value()
            if val == self.written.get(name):
                continue
            if name in COMMAND_TAGS:
                if val:
                    m.command(name.removeprefix("cmd_"))
                await self._write(name, False)
            elif name == "room_sp":
                m.command("setpoint", val)
                self.written[name] = val
            else:
                side = "GKS 6ж" if name == "key_sa_local" else "GKS 6е"
                setattr(m, name, bool(val))
                self.log("warn" if val else "info", f"Ключ {side} переведён в «{'Местное' if val else 'Дистанционное'}»")
                self.written[name] = bool(val)

    async def _write(self, name, value):
        now = datetime.now(timezone.utc)
        dv = ua.DataValue(ua.Variant(value, self.vtypes[name]), SourceTimestamp=now, ServerTimestamp=now)
        await self.server.write_attribute_value(self.nodes[name].nodeid, dv)
        self.written[name] = value

    async def publish(self):
        snap = self.model.snapshot()
        snap["fault_active"] = self.faults.text
        snap["last_event"] = self.last_event
        snap["heartbeat"] = self.heartbeat
        for name, value in snap.items():
            if self.written.get(name) != value:
                await self._write(name, value)

    async def flush_events(self):
        while self.pending_events:
            level, text = self.pending_events.pop(0)
            self.evgen.event.Severity = LEVEL[level][1]
            await self.evgen.trigger(message=text)

    # ---------------- основной цикл ----------------
    async def run(self):
        await self.build()
        a = self.args
        async with self.server:
            self._banner()
            if not a.no_autostart:
                self.model.command("start")
            if a.fault:
                self.faults.inject(a.fault)
            next_tick = time.monotonic()
            while True:
                await self.read_client_writes()
                left = TICK * a.speed
                while left > 1e-9:
                    dt = min(MAX_SUBSTEP, left)
                    self.faults.step(dt)
                    self.model.step(dt)
                    left -= dt
                self.heartbeat = (self.heartbeat + 1) % 2**32
                await self.publish()
                await self.flush_events()
                next_tick += TICK
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

    def _banner(self):
        a = self.args
        host = "localhost" if a.host in ("0.0.0.0", "") else a.host
        print("=" * 72)
        print(" ПВУ-1 — OPC UA эмулятор")
        print(f" Адрес:        opc.tcp://{host}:{a.port}/   (без шифрования, анонимный вход)")
        print(f" Пространство: {NS_URI}  → ns={self.idx}")
        print(f" Пример тега:  ns={self.idx};s=PVU1.room_temp")
        faults = "выкл." if a.no_faults else f"в среднем раз в {a.fault_interval:.0f} с модели"
        reset = "только оператором" if a.auto_reset <= 0 else f"через {a.auto_reset:.0f} с, если причина ушла"
        print(f" Ускорение:    ×{a.speed:g}   Неисправности: {faults}   Сброс блокировки: {reset}")
        print(" Остановить:   Ctrl+C")
        print("=" * 72, flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    try:
        asyncio.run(Emulator(parse_args()).run())
    except KeyboardInterrupt:
        print("\nЭмулятор остановлен")


if __name__ == "__main__":
    main()
