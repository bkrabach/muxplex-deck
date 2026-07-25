"""In-process Stream Deck+ emulator: virtual device + localhost HTTP control UI.

Emulates at the `device.DeckDevice` seam, not at USB/HID level -- the
physical HID layer is already hardware-proven by `deck_probe`. This module
makes everything *above* that layer (state machine, muxplex client,
rendering, interaction flow) fully real and testable with zero hardware:
no `streamdeck` library import, no hidapi, no `DeviceManager` construction.

`EmulatorDeviceManager` starts a background HTTP server (stdlib
`http.server`, threaded) that serves a small control UI and the same JSON
endpoints the UI itself polls/posts -- so a human on a Mac and an agent
running curl/httpx see identical behavior. The server runs for the whole
sidecar process lifetime: it *is* the virtual USB bus. "Unplug" and "plug"
just toggle a flag on the one virtual device; they don't tear the server
down, mirroring how a real USB bus stays powered while a cable is pulled.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PIL import Image

from .device import (
    DeckDevice,
    DeviceProbeError,
    DialCallback,
    DialEventType,
    KeyCallback,
    TouchCallback,
    TouchscreenEventType,
)

__all__ = ["EmulatorDevice", "EmulatorDeviceManager"]

logger = logging.getLogger("muxplex_deck")

KEY_COUNT = 8
KEY_LAYOUT = (2, 4)  # rows x cols, same as the real Stream Deck+
DIAL_COUNT = 4
KEY_SIZE = (120, 120)
STRIP_SIZE = (800, 100)
DEFAULT_PORT = 8484
# Real Elgato vendor id + the real Stream Deck+ USB product id (from the
# `streamdeck` library's own `USBVendorIDs`/`USBProductIDs`), so a capability
# report against the emulator shows the same "usb id" a real Stream Deck+
# would -- there's no meaningful "emulated" vendor/product id to invent, and
# this keeps `describe_capabilities()` output consistent between backends.
VENDOR_ID_ELGATO = 0x0FD9
PRODUCT_ID_STREAMDECK_PLUS = 0x84
# Simulates real hardware's dim firmware power-on default (real-hardware
# feedback: the deck never asserted brightness itself and stayed dim) --
# NOT the sidecar's target brightness. `main._run_active` calls
# `deck.set_brightness(FULL_BRIGHTNESS_PERCENT)` on every bring-up; starting
# the emulator below that value makes the call's effect observable via
# `snapshot_state()["brightness"]`.
INITIAL_BRIGHTNESS_PERCENT = 40


def _encode_jpeg(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=100)
    return buf.getvalue()


def _blank_key_jpeg() -> bytes:
    return _encode_jpeg(Image.new("RGB", KEY_SIZE, "black"))


def _blank_strip_jpeg() -> bytes:
    return _encode_jpeg(Image.new("RGB", STRIP_SIZE, "black"))


class EmulatorDevice:
    """A virtual Stream Deck+ satisfying the `DeckDevice` protocol.

    Same image formats (8x 120x120 JPEG keys, 800x100 JPEG touch strip) as
    the real StreamDeckPlus, so `rendering.py`'s `PILHelper`-based drawing
    works completely unchanged. `plugged` models the virtual USB cable --
    `connected()` reflects it directly; `is_open()` reflects our own
    open()/close() calls, independent of the cable, exactly like the real
    transport.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._is_open = False
        self.plugged = False
        self._brightness = INITIAL_BRIGHTNESS_PERCENT
        self._key_images: list[bytes] = [_blank_key_jpeg() for _ in range(KEY_COUNT)]
        self._strip_image: bytes = _blank_strip_jpeg()
        self._key_callback: KeyCallback | None = None
        self._dial_callback: DialCallback | None = None
        self._touch_callback: TouchCallback | None = None

    # --- DeckDevice protocol -------------------------------------------------

    def open(self) -> None:
        with self._lock:
            self._is_open = True

    def close(self) -> None:
        with self._lock:
            self._is_open = False

    def reset(self) -> None:
        with self._lock:
            self._key_images = [_blank_key_jpeg() for _ in range(KEY_COUNT)]
            self._strip_image = _blank_strip_jpeg()

    def is_open(self) -> bool:
        return self._is_open

    def connected(self) -> bool:
        return self.plugged

    def key_count(self) -> int:
        return KEY_COUNT

    def key_layout(self) -> tuple[int, int]:
        return KEY_LAYOUT

    def dial_count(self) -> int:
        return DIAL_COUNT

    def is_touch(self) -> bool:
        return True

    def touch_key_count(self) -> int:
        # The Plus's touch surface is the 800x100 touchscreen strip, not
        # discrete touch buttons (that's the Neo) -- zero, same as the
        # real Stream Deck+ reports.
        return 0

    def is_visual(self) -> bool:
        return True

    def vendor_id(self) -> int:
        return VENDOR_ID_ELGATO

    def product_id(self) -> int:
        return PRODUCT_ID_STREAMDECK_PLUS

    def deck_type(self) -> str:
        return "Stream Deck + (emulated)"

    def get_serial_number(self) -> str:
        return "EMULATED-0001"

    def get_firmware_version(self) -> str:
        return "emulator-1.0"

    def key_image_format(self) -> dict:
        return {
            "size": KEY_SIZE,
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    def touchscreen_image_format(self) -> dict:
        return {
            "size": STRIP_SIZE,
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    def set_brightness(self, percent: float) -> None:
        with self._lock:
            self._brightness = max(0, min(100, int(percent)))

    def set_key_image(self, key: int, image: bytes) -> None:
        with self._lock:
            self._key_images[key] = image

    def set_touchscreen_image(
        self,
        image: bytes,
        x_pos: int = 0,
        y_pos: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None:
        """Paint a region of the touch strip.

        Mirrors the real library's own validation: a non-empty image with a
        zero-size region is invalid (this is the exact bug fixed in
        `rendering._paint_full_touchscreen` -- see its docstring). Anything
        else is composited onto the current strip image, so partial-zone
        updates (like `deck_probe`'s dial counters) behave the same as on
        real hardware.
        """
        with self._lock:
            if not image:
                self._strip_image = _blank_strip_jpeg()
                return
            if width <= 0 or height <= 0:
                raise IndexError(f"Invalid draw width {width}")
            if (x_pos, y_pos, (width, height)) == (0, 0, STRIP_SIZE):
                self._strip_image = image
                return
            base = Image.open(io.BytesIO(self._strip_image)).convert("RGB")
            patch = Image.open(io.BytesIO(image)).convert("RGB")
            base.paste(patch, (x_pos, y_pos))
            self._strip_image = _encode_jpeg(base)

    def set_key_callback(self, callback: KeyCallback | None) -> None:
        with self._lock:
            self._key_callback = callback

    def set_dial_callback(self, callback: DialCallback | None) -> None:
        with self._lock:
            self._dial_callback = callback

    def set_touchscreen_callback(self, callback: TouchCallback | None) -> None:
        with self._lock:
            self._touch_callback = callback

    def __enter__(self) -> None:
        self._lock.acquire()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._lock.release()

    # --- HTTP-facing helpers (not part of DeckDevice) -------------------------

    def snapshot_state(self) -> dict:
        with self._lock:
            return {
                "plugged": self.plugged,
                "is_open": self._is_open,
                "brightness": self._brightness,
                "key_count": KEY_COUNT,
                "dial_count": DIAL_COUNT,
            }

    def key_image(self, index: int) -> bytes:
        with self._lock:
            return self._key_images[index]

    def strip_image(self) -> bytes:
        with self._lock:
            return self._strip_image

    def fire_key(self, key: int, pressed: bool) -> None:
        callback = self._key_callback
        if callback is not None:
            callback(self, key, pressed)

    def fire_dial(self, dial: int, event_type: DialEventType, value: object) -> None:
        callback = self._dial_callback
        if callback is not None:
            callback(self, dial, event_type, value)

    def fire_touch(self, event_type: TouchscreenEventType, value: dict) -> None:
        callback = self._touch_callback
        if callback is not None:
            callback(self, event_type, value)


_UI_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>muxplex-deck emulator</title>
<style>
  body { font-family: monospace; background: #111; color: #eee; }
  #keys { display: grid; grid-template-columns: repeat(4, 120px); gap: 8px; }
  #keys img { width: 120px; height: 120px; cursor: pointer; border: 1px solid #555; }
  #strip img { width: 800px; height: 100px; border: 1px solid #555; margin-top: 8px; cursor: crosshair; }
  button { margin: 2px; }
</style>
</head>
<body>
<h3>muxplex-deck emulator</h3>
<div id="status">loading...</div>
<button onclick="plug()">Plug in</button>
<button onclick="unplug()">Unplug</button>
<div id="keys"></div>
<div id="strip"><img id="stripimg" src="/strip.jpg"></div>
<h4>Dials</h4>
<div id="dials"></div>
<script>
const KEY_COUNT = 8;
const DIAL_COUNT = 4;

function buildKeys() {
  const div = document.getElementById('keys');
  for (let i = 0; i < KEY_COUNT; i++) {
    const img = document.createElement('img');
    img.id = 'key' + i;
    img.src = '/keys/' + i + '.jpg';
    img.onclick = () => clickKey(i);
    div.appendChild(img);
  }
}

function buildDials() {
  const div = document.getElementById('dials');
  for (let i = 0; i < DIAL_COUNT; i++) {
    const row = document.createElement('div');
    row.innerHTML = 'dial ' + i + ': ' +
      '<button onclick="turnDial(' + i + ', -1)">-</button>' +
      '<button onclick="turnDial(' + i + ', 1)">+</button>' +
      '<button onclick="pushDial(' + i + ')">push</button>';
    div.appendChild(row);
  }
}

async function post(path, body) {
  await fetch(path, { method: 'POST', body: JSON.stringify(body || {}) });
}

async function clickKey(i) { await post('/input/key', { key: i, action: 'click' }); }
async function turnDial(i, ticks) { await post('/input/dial', { dial: i, action: 'turn', ticks: ticks }); }
async function pushDial(i) {
  await post('/input/dial', { dial: i, action: 'push', pressed: true });
  await post('/input/dial', { dial: i, action: 'push', pressed: false });
}
async function plug() { await post('/plug'); }
async function unplug() { await post('/unplug'); }

async function refresh() {
  const t = Date.now();
  for (let i = 0; i < KEY_COUNT; i++) {
    document.getElementById('key' + i).src = '/keys/' + i + '.jpg?t=' + t;
  }
  document.getElementById('stripimg').src = '/strip.jpg?t=' + t;
  try {
    const res = await fetch('/state');
    const state = await res.json();
    document.getElementById('status').textContent =
      'plugged=' + state.plugged + ' open=' + state.is_open +
      ' brightness=' + state.brightness + '% keys=' + state.key_count;
  } catch (e) {
    document.getElementById('status').textContent = 'server unreachable?';
  }
}

document.getElementById('stripimg').onclick = (e) => {
  const rect = e.target.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) * (800 / rect.width));
  const y = Math.round((e.clientY - rect.top) * (100 / rect.height));
  post('/input/touch', { type: 'short', x: x, y: y });
};

buildKeys();
buildDials();
refresh();
setInterval(refresh, 400);
</script>
</body>
</html>
"""


class _EmulatorHandler(BaseHTTPRequestHandler):
    """Serves the control UI + JSON/JPEG endpoints for one `EmulatorDevice`.

    `device` and `manager` are class attributes, bound per-server by
    `_bound_handler_class()` below -- `BaseHTTPRequestHandler` instances are
    constructed by `HTTPServer` internals with a fixed signature, so this is
    the standard way to hand instance-specific state to request handlers.
    """

    device: EmulatorDevice
    manager: EmulatorDeviceManager

    def log_message(self, format: str, *args: object) -> None:
        pass  # the sidecar's own logger already covers what matters

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_bytes(200, "text/html; charset=utf-8", _UI_HTML.encode("utf-8"))
            return
        if path == "/state":
            self._send_json(200, self.device.snapshot_state())
            return
        if path == "/strip.jpg":
            self._send_bytes(200, "image/jpeg", self.device.strip_image())
            return
        if path.startswith("/keys/") and path.endswith(".jpg"):
            try:
                index = int(path[len("/keys/") : -len(".jpg")])
                self._send_bytes(200, "image/jpeg", self.device.key_image(index))
            except (ValueError, IndexError):
                self._send_json(404, {"error": "unknown key index"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/plug":
                self.manager.plug()
                self._send_json(200, self.device.snapshot_state())
            elif path == "/unplug":
                self.manager.unplug()
                self._send_json(200, self.device.snapshot_state())
            elif path == "/input/key":
                self._handle_key_input(self._read_json())
                self._send_json(200, {"ok": True})
            elif path == "/input/dial":
                self._handle_dial_input(self._read_json())
                self._send_json(200, {"ok": True})
            elif path == "/input/touch":
                self._handle_touch_input(self._read_json())
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "not found"})
        except (KeyError, ValueError, TypeError) as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_key_input(self, body: dict) -> None:
        key = int(body["key"])
        action = body.get("action", "click")
        if action == "press":
            self.device.fire_key(key, True)
        elif action == "release":
            self.device.fire_key(key, False)
        elif action == "click":
            self.device.fire_key(key, True)
            self.device.fire_key(key, False)
        else:
            raise ValueError(f"unknown key action {action!r}")

    def _handle_dial_input(self, body: dict) -> None:
        dial = int(body["dial"])
        action = body.get("action")
        if action == "turn":
            self.device.fire_dial(dial, DialEventType.TURN, int(body["ticks"]))
        elif action == "push":
            self.device.fire_dial(dial, DialEventType.PUSH, bool(body["pressed"]))
        else:
            raise ValueError(f"unknown dial action {action!r}")

    def _handle_touch_input(self, body: dict) -> None:
        kind = body["type"]
        if kind == "short":
            value = {"x": int(body["x"]), "y": int(body["y"])}
            self.device.fire_touch(TouchscreenEventType.SHORT, value)
        elif kind == "long":
            value = {"x": int(body["x"]), "y": int(body["y"])}
            self.device.fire_touch(TouchscreenEventType.LONG, value)
        elif kind == "drag":
            value = {
                "x": int(body["x"]),
                "y": int(body["y"]),
                "x_out": int(body["x_out"]),
                "y_out": int(body["y_out"]),
            }
            self.device.fire_touch(TouchscreenEventType.DRAG, value)
        else:
            raise ValueError(f"unknown touch type {kind!r}")


def _bound_handler_class(
    device: EmulatorDevice, manager: EmulatorDeviceManager
) -> type[_EmulatorHandler]:
    """Create a `_EmulatorHandler` subclass bound to one device+manager pair."""
    return type(
        "BoundEmulatorHandler",
        (_EmulatorHandler,),
        {"device": device, "manager": manager},
    )


class EmulatorDeviceManager:
    """Backend-level 'find device' seam for the emulator -- the virtual USB bus.

    The HTTP server runs for the lifetime of this manager (i.e. the whole
    sidecar process); unplug/plug toggle a flag on the single virtual
    device rather than tearing the server down.
    """

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.device = EmulatorDevice()
        handler_class = _bound_handler_class(self.device, self)
        try:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
        except OSError as exc:
            raise DeviceProbeError(
                f"Could not bind emulator UI to 127.0.0.1:{port}: {exc}\n"
                "Pass a different port with --emulator-port <N>."
            ) from exc
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        logger.info("emulator UI: http://127.0.0.1:%d", self.port)

    def find_device(self) -> DeckDevice | None:
        return self.device if self.device.plugged else None

    def plug(self) -> None:
        self.device.plugged = True

    def unplug(self) -> None:
        self.device.plugged = False

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
