"""Choices for the panel's pickers. Every lookup is read-only and failure-tolerant:
an error is reported next to the list and the field stays a plain text box."""
from __future__ import annotations

# field kind -> HA domains
DOMAINS = {
    "binary_sensor": ("binary_sensor",), "zone": ("zone",), "input_select": ("input_select",),
    "input_datetime": ("input_datetime",), "sensor": ("sensor",), "switch": ("switch",),
    "device_tracker": ("device_tracker",), "input_number": ("input_number",),
}

DEV_ENTITIES = [
    ("binary_sensor.test_occupancy", "Test zone occupancy"), ("zone.test_carport", "Test carport"),
    ("input_select.t_vehicle", "Test latch vehicle"), ("input_select.t_source", "Test latch source"),
    ("input_datetime.t_latched_at", "Test latched at"), ("sensor.charger_status", "Charger status"),
    ("sensor.charger_energy_session", "Charger session energy"), ("switch.charger_availability", "Charger availability"),
    ("switch.charger_charge_control", "Charger charge control"),
    ("device_tracker.alpha_location", "Alpha location"), ("input_number.alpha_kwh", "Alpha kWh"),
    ("light.kitchen", "Kitchen"),
]


def notify_from_services(raw: list[dict]) -> list[dict]:
    """HA /api/services -> notify.<service> choices (notify targets are services, not entities)."""
    out = []
    for d in raw or []:
        if d.get("domain") == "notify":
            for name in sorted((d.get("services") or {})):
                out.append({"value": f"notify.{name}", "label": name})
    return out


def devices_from_registry(raw: list[dict]) -> list[dict]:
    """Device registry -> the mobile_app devices (phones). `value` is the registry id, which is
    the device_id carried by tag-scan events."""
    out = []
    for d in raw or []:
        ids = d.get("identifiers") or []
        if any(i and i[0] == "mobile_app" for i in ids):
            label = d.get("name_by_user") or d.get("name") or d["id"]
            model = " ".join(x for x in (d.get("manufacturer"), d.get("model")) if x)
            out.append({"value": d["id"], "label": f"{label} ({model})" if model else label})
    return sorted(out, key=lambda x: x["label"].lower())


def build(ha, seen: dict, dev: bool) -> dict:
    """Everything the panel's pickers need, in one response."""
    errors: dict[str, str] = {}
    ents: dict[str, list] = {k: [] for k in DOMAINS}
    notify, devices = [], []
    if dev and ha is None:
        for eid, name in DEV_ENTITIES:
            for k, doms in DOMAINS.items():
                if eid.split(".")[0] in doms:
                    ents[k].append({"value": eid, "label": name})
        notify = [{"value": "notify.mobile_app_ann", "label": "mobile_app_ann"}]
        devices = [{"value": "dev_ann", "label": "Ann's phone (Apple iPhone)"}]
    elif ha is not None:
        try:
            for e in ha.list_entities():
                dom = e["entity_id"].split(".")[0]
                for k, doms in DOMAINS.items():
                    if dom in doms:
                        ents[k].append({"value": e["entity_id"], "label": e["name"] or e["entity_id"]})
        except Exception as ex:      # noqa: BLE001
            errors["entities"] = str(ex)
        try:
            notify = notify_from_services(ha.services())
        except Exception as ex:      # noqa: BLE001
            errors["notify"] = str(ex)
        try:
            devices = devices_from_registry(ha.device_registry())
        except Exception as ex:      # noqa: BLE001
            errors["devices"] = str(ex)
    else:
        errors["ha"] = "Home Assistant is not connected"
    frigate = {k: sorted(v) for k, v in seen.items()}
    return {"entities": ents, "notify": notify, "devices": devices, "frigate": frigate, "errors": errors}
