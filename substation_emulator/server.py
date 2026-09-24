"""OPC UA сервер эмулятора ПС 110/10 кВ (библиотека asyncua)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from asyncua import Server, ua
from asyncua.common.event_objects import BaseEvent
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.address_space import AttributeService

from .model import Model
from .tags import FEEDERS, LINES, SECTIONS, TAGS, TAGS_BY_PATH, TRANSFORMERS, Tag

log = logging.getLogger("substation")

NAMESPACE_URI = "urn:substation-emulator"
VARIANT_TYPES = {
    "Double": ua.VariantType.Double,
    "Boolean": ua.VariantType.Boolean,
    "Int32": ua.VariantType.Int32,
    "UInt32": ua.VariantType.UInt32,
    "String": ua.VariantType.String,
}

# Описания объектов-папок в адресном пространстве
FOLDERS = {
    "Station": "Общестанционные сигналы",
    "SecCB": "Секционный выключатель 10 кВ (СВ)",
    "ABR": "Автоматический ввод резерва 10 кВ",
    "DS": "Линейный разъединитель",
    "CB": "Выключатель",
    "InCB": "Вводной выключатель",
    "Tap": "Устройство РПН",
    "AVR": "Автоматический регулятор напряжения трансформатора (АРНТ)",
    "Cooling": "Система охлаждения",
    "Alarm": "Сигнализация",
    "Prot": "Релейная защита (МТЗ)",
}
FOLDERS.update({f"VL{n}": t for n, t in LINES.items()})
FOLDERS.update({f"T{n}": t for n, t in TRANSFORMERS.items()})
FOLDERS.update({f"Sec{n}": t for n, t in SECTIONS.items()})
FOLDERS.update({tag: spec[0] for tag, spec in FEEDERS.items()})


class _ValidatingAttributeService(AttributeService):
    """Перехватывает записи клиентов в теги эмулятора: проверка диапазона, передача в модель."""

    def __init__(self, aspace, emulator: "SubstationServer"):
        super().__init__(aspace)
        self._emu = emulator

    async def write(self, params: ua.WriteParameters, user: User = User(role=UserRole.Admin)) -> list[ua.StatusCode]:
        results: list[ua.StatusCode] = []
        for wv in params.NodesToWrite:
            tag = self._emu.tag_for(wv.NodeId) if wv.AttributeId == ua.AttributeIds.Value else None
            if tag is None:
                results += await super().write(ua.WriteParameters(NodesToWrite=[wv]), user)
                continue
            raw = wv.Value.Value.Value if wv.Value is not None and wv.Value.Value is not None else None
            value, err = self._emu.model.check(tag.path, raw)
            if err:
                log.warning("Запись %s = %r отклонена: %s", tag.path, raw, err)
                results.append(ua.StatusCode(getattr(ua.StatusCodes, err)))
                continue
            # Приводим к типу тега (клиент может прислать Float/Int в Double и т.п.)
            dv = dataclasses.replace(wv.Value, Value=ua.Variant(value, VARIANT_TYPES[tag.type]))
            res = await super().write(ua.WriteParameters(NodesToWrite=[dataclasses.replace(wv, Value=dv)]), user)
            if res[0].is_good():
                self._emu.on_client_write(tag, value)
            results += res
        return results


class SubstationServer:
    def __init__(self, model: Model, host: str = "0.0.0.0", port: int = 4840, tick: float = 0.1,
                 setpoints_file: str | None = "setpoints.json"):
        self.model = model
        self.endpoint = f"opc.tcp://{host}:{port}/substation/"
        self.tick = tick
        self.setpoints_file = Path(setpoints_file) if setpoints_file else None
        self.server = Server()
        self.ns = 0
        self._nodes: dict[str, ua.NodeId] = {}
        self._by_nodeid: dict[ua.NodeId, Tag] = {}
        self._published: dict[str, object] = {}
        self._evgen = None
        self.ready = asyncio.Event()

    def tag_for(self, nodeid: ua.NodeId) -> Tag | None:
        return self._by_nodeid.get(nodeid)

    def on_client_write(self, tag: Tag, value: object) -> None:
        self.model.write(tag.path, value)
        self._published[tag.path] = value
        log.info("Клиент: %s = %r", tag.path, value)

    async def init(self) -> None:
        if self.setpoints_file and self.setpoints_file.exists():
            try:
                self.model.load_setpoints(json.loads(self.setpoints_file.read_text(encoding="utf-8")))
                log.info("Уставки загружены из %s", self.setpoints_file)
            except (OSError, ValueError) as e:
                log.warning("Не удалось прочитать %s: %s — используются уставки по умолчанию", self.setpoints_file, e)

        srv = self.server
        await srv.init()
        srv.set_endpoint(self.endpoint)
        srv.set_server_name("Substation 110/10 kV Emulator")
        await srv.set_application_uri("urn:substation-emulator:server")
        srv.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await srv.register_namespace(NAMESPACE_URI)
        srv.iserver.attribute_service = _ValidatingAttributeService(srv.iserver.aspace, self)

        root = await srv.nodes.objects.add_object(ua.NodeId("Substation", self.ns),
                                                  ua.QualifiedName("Substation", self.ns))
        await self._describe(root, "Подстанция 110/10 кВ (эмулятор)")
        folders = {"": root}
        for tag in TAGS:
            parts = tag.path.split(".")
            parent = root
            for i in range(1, len(parts)):
                key = ".".join(parts[:i])
                if key not in folders:
                    folders[key] = await parent.add_object(ua.NodeId(key, self.ns),
                                                           ua.QualifiedName(parts[i - 1], self.ns))
                    await self._describe(folders[key], FOLDERS.get(parts[i - 1], parts[i - 1]))
                parent = folders[key]
            await self._add_variable(parent, tag)

        self._evgen = await srv.get_event_generator(BaseEvent(sourcenode=root.nodeid))

    async def _describe(self, node, text: str) -> None:
        await node.write_attribute(ua.AttributeIds.Description,
                                   ua.DataValue(ua.Variant(ua.LocalizedText(text, "ru-RU"))))

    async def _add_variable(self, parent, tag: Tag) -> None:
        name = tag.path.rsplit(".", 1)[-1]
        nodeid = ua.NodeId(tag.path, self.ns)
        value = self.model.values[tag.path]
        node = await parent.add_variable(nodeid, ua.QualifiedName(name, self.ns),
                                         ua.Variant(value, VARIANT_TYPES[tag.type]))
        await self._describe(node, tag.desc + (f", {tag.unit}" if tag.unit else ""))
        if tag.unit:
            eu = ua.EUInformation(NamespaceUri="http://www.opcfoundation.org/UA/units/un/cefact", UnitId=-1,
                                  DisplayName=ua.LocalizedText(tag.unit), Description=ua.LocalizedText(tag.unit))
            await node.add_property(ua.NodeId(f"{tag.path}.EngineeringUnits", self.ns),
                                    ua.QualifiedName("EngineeringUnits", 0), eu)
        if tag.lo is not None:
            await node.add_property(ua.NodeId(f"{tag.path}.EURange", self.ns),
                                    ua.QualifiedName("EURange", 0), ua.Range(Low=tag.lo, High=tag.hi))
        if tag.writable:
            await node.set_writable(True)
        self._nodes[tag.path] = nodeid
        self._by_nodeid[nodeid] = tag
        self._published[tag.path] = value

    async def _publish(self) -> None:
        now = datetime.now(timezone.utc)
        for path, value in self.model.values.items():
            if self._published.get(path) == value:
                continue
            self._published[path] = value
            tag = TAGS_BY_PATH[path]
            dv = ua.DataValue(ua.Variant(value, VARIANT_TYPES[tag.type]), SourceTimestamp=now, ServerTimestamp=now)
            await self.server.write_attribute_value(self._nodes[path], dv)
        for severity, text in self.model.pop_events():
            log.log(logging.WARNING if severity >= 500 else logging.INFO, "Событие: %s", text)
            self._evgen.event.Severity = severity
            await self._evgen.trigger(message=text)

    def _save_setpoints(self) -> None:
        if not (self.setpoints_file and self.model.setpoints_dirty):
            return
        self.model.setpoints_dirty = False
        tmp = self.setpoints_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.model.setpoints(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.setpoints_file)
        except OSError as e:
            log.warning("Не удалось сохранить уставки в %s: %s", self.setpoints_file, e)

    async def run(self) -> None:
        await self.init()
        loop = asyncio.get_running_loop()
        async with self.server:
            log.info("OPC UA сервер запущен: %s (namespace ns=%d «%s», тегов: %d)",
                     self.endpoint, self.ns, NAMESPACE_URI, len(TAGS))
            self.ready.set()
            last = loop.time()
            while True:
                now = loop.time()
                self.model.step(min(now - last, 1.0))
                last = now
                await self._publish()
                self._save_setpoints()
                await asyncio.sleep(max(0.0, self.tick - (loop.time() - now)))
