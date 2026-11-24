"""A connected input source shown in the shared remote workbench."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeDevice:
    id: tuple
    name: str
    transport: str
    vendor: int
    product: int
    unit_key: str = ""
    connection: tuple[int, ...] = ()
    vendor_id_source: int | None = None
    bcd_device: int | None = None

    @property
    def default_remote_name(self) -> str:
        """Name a newly added remote by its hardware ID, never by the name the device reports."""
        if not (self.vendor or self.product):
            return "遥控器"
        return f"遥控器 {self.vendor:04X}:{self.product:04X}"


@dataclass(frozen=True)
class UnlistedDevice:
    """A connected device without a readable key interface, kept to explain why."""
    name: str
    transport: str          # "USB", "蓝牙" or "" when the system does not say
    vendor: int
    product: int
    collections: frozenset = frozenset()   # application collections as (page, usage)
    pages: frozenset = frozenset()         # usage pages with input data
    hid: bool = True                       # False: seen on USB with no HID interface at all
