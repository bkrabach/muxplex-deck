"""`muxplex_deck.usbnode` -- pure sysfs facts, fake trees under `tmp_path`.

No hardware, no WSL, no subprocess: every default path is injectable, so
these tests build a fake `/sys/bus/usb/devices` tree and a fake
`/dev/bus/usb` tree under `tmp_path` and never touch the real filesystem
outside it.
"""

from __future__ import annotations

import os
from pathlib import Path

from muxplex_deck import usbnode


def _make_sysfs_entry(
    sysfs_root: Path,
    name: str,
    *,
    vendor_id: str,
    product_id: str = "006d",
    busnum: int = 1,
    devnum: int = 3,
) -> None:
    entry = sysfs_root / name
    entry.mkdir(parents=True)
    (entry / "idVendor").write_text(f"{vendor_id}\n", encoding="utf-8")
    (entry / "idProduct").write_text(f"{product_id}\n", encoding="utf-8")
    (entry / "busnum").write_text(f"{busnum}\n", encoding="utf-8")
    (entry / "devnum").write_text(f"{devnum}\n", encoding="utf-8")


class TestFindUsbNode:
    def test_matching_device_found(self, tmp_path: Path) -> None:
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        _make_sysfs_entry(sysfs_root, "1-4", vendor_id="0fd9", busnum=1, devnum=3)
        node_path = dev_root / "001" / "003"
        node_path.parent.mkdir(parents=True)
        node_path.write_text("", encoding="utf-8")
        os.chmod(node_path, 0o660)

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is not None
        assert node.path == node_path
        assert node.vendor_id == "0fd9"
        assert node.product_id == "006d"
        assert node.busnum == 1
        assert node.devnum == 3
        assert node.mode == 0o660

    def test_case_insensitive_vendor_match(self, tmp_path: Path) -> None:
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        _make_sysfs_entry(sysfs_root, "1-4", vendor_id="0FD9")

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is not None

    def test_no_matching_vendor_returns_none(self, tmp_path: Path) -> None:
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        _make_sysfs_entry(sysfs_root, "1-4", vendor_id="1234")

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is None

    def test_missing_sysfs_root_returns_none(self, tmp_path: Path) -> None:
        node = usbnode.find_usb_node(
            "0fd9",
            sysfs_root=tmp_path / "nonexistent",
            dev_root=tmp_path / "dev",
        )
        assert node is None

    def test_partial_attrs_skipped_not_raised(self, tmp_path: Path) -> None:
        """An entry with idVendor but no idProduct/busnum/devnum must be
        skipped, not raise -- sysfs entries can be transiently incomplete.
        """
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        entry = sysfs_root / "1-4"
        entry.mkdir(parents=True)
        (entry / "idVendor").write_text("0fd9\n", encoding="utf-8")
        # idProduct/busnum/devnum deliberately missing

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is None

    def test_unreadable_node_reports_mode_none_and_not_accessible(
        self, tmp_path: Path
    ) -> None:
        """The device node itself doesn't exist yet (e.g. right after
        attach, before the kernel creates it) -- mode/owner are None, but
        the node's PATH is still resolved from sysfs (real value, per P5).
        """
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        _make_sysfs_entry(sysfs_root, "1-4", vendor_id="0fd9", busnum=1, devnum=3)
        # No node written under dev_root.

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is not None
        assert node.path == dev_root / "001" / "003"
        assert node.mode is None
        assert node.owner_uid is None
        assert node.readable_writable is False

    def test_accessible_node_reports_readable_writable_true(
        self, tmp_path: Path
    ) -> None:
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        _make_sysfs_entry(sysfs_root, "1-4", vendor_id="0fd9", busnum=1, devnum=3)
        node_path = dev_root / "001" / "003"
        node_path.parent.mkdir(parents=True)
        node_path.write_text("", encoding="utf-8")
        os.chmod(node_path, 0o666)

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is not None
        assert node.readable_writable is True

    def test_node_padding_is_three_digits(self, tmp_path: Path) -> None:
        sysfs_root = tmp_path / "sys"
        dev_root = tmp_path / "dev"
        _make_sysfs_entry(sysfs_root, "1-4", vendor_id="0fd9", busnum=1, devnum=42)

        node = usbnode.find_usb_node("0fd9", sysfs_root=sysfs_root, dev_root=dev_root)

        assert node is not None
        assert node.path == dev_root / "001" / "042"


class TestUdevIsLive:
    def test_control_socket_present_is_live(self, tmp_path: Path) -> None:
        control = tmp_path / "control"
        control.write_text("", encoding="utf-8")
        assert usbnode.udev_is_live(control_path=control) is True

    def test_control_socket_absent_is_not_live(self, tmp_path: Path) -> None:
        assert usbnode.udev_is_live(control_path=tmp_path / "nonexistent") is False
