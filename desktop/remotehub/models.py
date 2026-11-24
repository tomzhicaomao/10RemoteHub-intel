"""Device model registry: identity rules plus what the remote looks like and reports.

Each JSON file under models/ describes one remote:
  identity      vid/pid, optional bcdDevice and serial prefix
  presentation  canvas, illustration, hit regions (source pixels)
  behavior      keymap file with the HID triggers
  enrollment    key that confirms the preset, and verification zones: physical
                key groups in check order, the keys that must pass in each,
                and keys checked as press-hold-release
  voice         model-specific microphone information

Enrollment treats a matching identity as a candidate until the user tries the
specified key. The fixed collector uses its own USB inventory match.
"""
from dataclasses import dataclass
import json
from pathlib import Path

DIRECTORY = Path(__file__).parent / "models"
USB_RECEIVER = "usb-receiver"
HID_TRANSPORTS = {"USB": USB_RECEIVER, "蓝牙": "bluetooth-hid"}


@dataclass(frozen=True)
class Zone:
    name: str
    keys: tuple
    required: tuple = ()
    hold: tuple = ()


def _zones(raw):
    zones = []
    for item in raw or ():
        keys = tuple(str(k) for k in item["keys"])
        required = tuple(str(k) for k in item.get("required", ()))
        hold = tuple(str(k) for k in item.get("hold", ()))
        if not keys or not set(required) <= set(keys) or not set(hold) <= set(keys):
            raise ValueError("验证分区无效")
        zones.append(Zone(str(item["name"]), keys, required, hold))
    return tuple(zones)


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    transport: str
    vendor: int
    product: int
    bcd_device: int | None
    serial_prefix: str
    canvas: tuple
    image: str
    regions: tuple
    keymap: str
    voice: dict
    schema: int = 1
    button_id_prefix: str = ""
    confirmation_button: str = ""
    vendor_id_source: int | None = None
    zones: tuple = ()   # (Zone, ...) in verification order

    @property
    def pair(self):
        return self.vendor, self.product


def load(directory=DIRECTORY):
    models = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            identity = data["identity"]
            presentation = data.get("presentation", {})
            behavior = data.get("behavior", {})
            vendor_id_source = identity.get("vendor_id_source")
            if vendor_id_source is not None and (type(vendor_id_source) is not int or vendor_id_source not in (1, 2)):
                continue
            models.append(Model(
                id=str(data["id"]),
                name=str(data.get("name", data["id"])),
                transport=str(data.get("transport", USB_RECEIVER)),
                vendor=int(identity["vendor"]),
                product=int(identity["product"]),
                bcd_device=identity.get("bcd_device"),
                serial_prefix=str(identity.get("serial_prefix", "")),
                canvas=tuple(presentation.get("canvas", (793, 1983))),
                image=str(presentation.get("image", "")),
                regions=tuple(tuple(region) for region in presentation.get("regions", ())),
                keymap=str(behavior.get("keymap", "")),
                voice=dict(data.get("voice", {})),
                schema=int(data.get("schema", 1)),
                button_id_prefix=str(behavior.get("button_id_prefix", "")),
                confirmation_button=str(data.get("enrollment", {}).get("confirmation_button", "")),
                vendor_id_source=vendor_id_source,
                zones=_zones(data.get("enrollment", {}).get("zones")),
            ))
        except (OSError, AttributeError, KeyError, TypeError, ValueError):
            continue  # a broken pack must not take the app down
    return models


def by_pair(vendor, product, models=None):
    for model in (models if models is not None else load()):
        if model.transport == USB_RECEIVER and model.pair == (vendor, product):
            return model
    return None


def supported_pairs(models=None):
    return {model.pair for model in (models if models is not None else load()) if model.transport == USB_RECEIVER}


def fingerprint(device):
    """Stable, human-readable identity for logs and adaptation reports."""
    parts = [f"USB {device.vendor:04X}:{device.product:04X}"]
    bcd = getattr(device, "bcd_device", None)
    if bcd is not None:
        parts.append(f"bcd {bcd:04X}")
    serial = getattr(device, "serial", "") or ""
    if serial:
        parts.append("serial " + serial.split("_")[0])
    return " · ".join(parts)


def _matches_identity(model, device):
    bcd = getattr(device, "bcd_device", None)
    return model.pair == (device.vendor, device.product) and (
        model.bcd_device is None or bcd is None or bcd == model.bcd_device
    )


def match(device, models=None):
    """Match a model in the fixed USB collector's inventory."""
    return next((model for model in (models if models is not None else load())
                 if model.transport == USB_RECEIVER and _matches_identity(model, device)), None)


def enrollment_candidates(device, models):
    """Find presets whose identity is consistent with this HID device."""
    transport = HID_TRANSPORTS.get(device.transport)
    source = getattr(device, "vendor_id_source", None)
    return [
        model for model in models
        if model.confirmation_button and model.transport == transport and _matches_identity(model, device)
        and (model.vendor_id_source is None or model.vendor_id_source == source)
    ]


def same_dongle(device, models=None):
    """Known vid/pid but no model matches: another remote on the same scheme."""
    return (device.vendor, device.product) in supported_pairs(models) and match(device, models) is None
