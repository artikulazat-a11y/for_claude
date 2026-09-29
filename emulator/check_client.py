"""Проверочный OPC UA клиент для эмулятора ПВУ-1.

Примеры:
  python check_client.py                         — прочитать все теги
  python check_client.py --watch                 — следить за состоянием и авариями
  python check_client.py --cmd start             — команда через тег cmd_start
  python check_client.py --sp 22.5               — уставка через метод SetSetpoint
  python check_client.py --fault pump_trip       — включить неисправность (clear — снять)
"""

import argparse
import asyncio
import logging
import sys

from asyncua import Client, ua

from pvu_tags import TAGS

NS_URI = "urn:pvu1:emulator"


def fmt(v):
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


class EventPrinter:
    def event_notification(self, event):
        print(f"  событие [{event.Severity}]: {event.Message.Text}", flush=True)


async def main():
    p = argparse.ArgumentParser(description="Проверочный клиент эмулятора ПВУ-1")
    p.add_argument("--url", default="opc.tcp://localhost:4891/")
    p.add_argument("--cmd", choices=["start", "stop", "reset", "replace_filter"])
    p.add_argument("--sp", type=float, help="уставка температуры, °C")
    p.add_argument("--fault", help="имя неисправности или clear")
    p.add_argument("--watch", action="store_true", help="выводить состояние каждые 2 с")
    a = p.parse_args()

    async with Client(a.url) as client:
        idx = await client.get_namespace_index(NS_URI)
        node = lambda name: client.get_node(ua.NodeId(f"PVU1.{name}", idx))
        root = client.get_node(ua.NodeId("PVU1", idx))

        if a.cmd:
            await node(f"cmd_{a.cmd}").write_value(ua.Variant(True, ua.VariantType.Boolean))
            print(f"Записано cmd_{a.cmd} = true")
        if a.sp is not None:
            ok = await root.call_method(ua.NodeId("PVU1.SetSetpoint", idx), ua.Variant(a.sp, ua.VariantType.Double))
            print(f"SetSetpoint({a.sp}) → {ok}")
        if a.fault:
            ok = await root.call_method(ua.NodeId("PVU1.InjectFault", idx), a.fault)
            print(f"InjectFault({a.fault}) → {ok}")

        if not a.watch:
            await asyncio.sleep(0.5)
            print(f"{'тег':<20} {'значение':>12}  описание")
            for name, _folder, _typ, unit, desc, _w in TAGS:
                v = await node(name).read_value()
                print(f"{name:<20} {fmt(v):>12}  {unit:<5} {desc}")
            return

        sub = await client.create_subscription(500, EventPrinter())
        await sub.subscribe_events(client.nodes.server)
        keys = ["unit_state", "room_temp", "room_sp", "air_heater_temp", "water_return_temp", "valve_pos",
                "filter_dp", "fan_sa_dp", "fan_ea_dp", "alarm_count", "fault_active"]
        while True:
            vals = {k: await node(k).read_value() for k in keys}
            print(f"{vals['unit_state']:<22} помещ. {vals['room_temp']:5.1f}/{vals['room_sp']:.1f} °C  "
                  f"за калор. {vals['air_heater_temp']:5.1f}  обратка {vals['water_return_temp']:5.1f}  "
                  f"клапан {vals['valve_pos']:5.1f}%  ΔP ф/п/в {vals['filter_dp']:.2f}/{vals['fan_sa_dp']:.2f}/{vals['fan_ea_dp']:.2f}  "
                  f"аварий {vals['alarm_count']}  {vals['fault_active']}", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
