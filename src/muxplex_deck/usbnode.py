"""Pure Linux USB facts -- no WSL knowledge, no subprocess, no prose.

Resolves a live USB device node from sysfs (kernel-provided, version-
independent) and checks whether udev is actually running, both by reading
the filesystem only. Every default path is an injectable parameter so
tests can build a fake tree under ``tmp_path`` -- nothing here can touch a
real device, so no new autouse safety rail is needed for this module.

Deliberately does **not** use ``StreamDeck``'s ``device_info['path']``:
that is hidapi's ``bus:addr:iface`` hex string, whose format varies across
hidapi releases. sysfs's ``idVendor``/``idProduct``/``busnum``/``devnum``
files are kernel ABI and don't drift with a library upgrade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["UsbNode", "find_usb_node", "udev_is_live"]


@dataclass(frozen=True)
class UsbNode:
    """A USB device node resolved from sysfs for a given vendor id."""

    path: Path
    vendor_id: str
    product_id: str
    busnum: int
    devnum: int
    mode: int | None
    owner_uid: int | None
    readable_writable: bool


def find_usb_node(
    vendor_id: str = "0fd9",
    *,
    sysfs_root: Path = Path("/sys/bus/usb/devices"),
    dev_root: Path = Path("/dev/bus/usb"),
) -> UsbNode | None:
    """Find the first USB device matching `vendor_id` and its `/dev` node.

    Iterates `sysfs_root/*/idVendor` (case-insensitive match), reads
    `idProduct`/`busnum`/`devnum` for the matching entry, and builds
    `dev_root / f"{busnum:03d}" / f"{devnum:03d}"`. Returns `None` on any
    `OSError` (missing sysfs, permission issues) or if nothing matches.
    Never raises.
    """
    vendor_id_lower = vendor_id.lower()
    try:
        entries = sorted(sysfs_root.iterdir())
    except OSError:
        return None

    for entry in entries:
        try:
            found_vendor = (
                (entry / "idVendor").read_text(encoding="utf-8").strip().lower()
            )
        except OSError:
            continue
        if found_vendor != vendor_id_lower:
            continue

        try:
            product_id = (entry / "idProduct").read_text(encoding="utf-8").strip()
            busnum = int((entry / "busnum").read_text(encoding="utf-8").strip())
            devnum = int((entry / "devnum").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue

        node_path = dev_root / f"{busnum:03d}" / f"{devnum:03d}"

        mode: int | None = None
        owner_uid: int | None = None
        try:
            st = node_path.stat()
            mode = st.st_mode & 0o777
            owner_uid = st.st_uid
        except OSError:
            pass

        return UsbNode(
            path=node_path,
            vendor_id=found_vendor,
            product_id=product_id,
            busnum=busnum,
            devnum=devnum,
            mode=mode,
            owner_uid=owner_uid,
            readable_writable=os.access(node_path, os.R_OK | os.W_OK),
        )

    return None


def udev_is_live(*, control_path: Path = Path("/run/udev/control")) -> bool:
    """Cheap, non-root, deterministic probe: will a udev rule ever fire here?

    `udevadm control --reload-rules` talks to this socket; when it is
    absent (WSL without systemd, most containers), the reload fails with
    "No such file or directory" and rules are silently never applied. Any
    user can `stat()` this path even though the socket itself is
    `srw-------`, because the containing directory is world-readable.
    """
    return control_path.exists()
