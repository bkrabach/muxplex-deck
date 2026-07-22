# muxplex-deck: Stream Deck+ hardware probe

A PoC/spike app that proves we can drive every feature of an Elgato Stream
Deck+ (8 LCD keys, 4 push-dial encoders, 800x100 touch strip) and capture
every input it can produce, including clean hotplug (unplug/replug without
restarting the process). It is the seed of a future muxplex sidecar --
this probe does device I/O only, no server/network integration.

## macOS setup

1. Install [uv](https://docs.astral.sh/uv/) if you don't have it:
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Install the native HIDAPI library (the `streamdeck` library needs it at runtime):
   ```sh
   brew install hidapi
   ```
3. **Quit the official Elgato Stream Deck app first.** It holds exclusive
   access to the device's USB HID interface -- if it's running, this probe
   will not be able to open the device.
4. From this directory, sync dependencies:
   ```sh
   uv sync
   ```

## Running

```sh
uv run deck-probe
```

Or equivalently:

```sh
uv run python -m deck_probe
```

Press `Ctrl+C` to exit cleanly at any time -- the deck is reset (blanked)
and the device handle is closed before the process exits.

## Verification checklist

Walk through these in order. Everything is logged to the console with a
timestamp; watch both the terminal and the physical device.

1. **Cold start, no device connected**
   - Run `uv run deck-probe` with nothing plugged in.
   - Expect: `waiting for Stream Deck+ (polling every 2s)...` logged once,
     not repeated every 2 seconds (a "still waiting" heartbeat is fine
     roughly every 30s).

2. **Plug in the Stream Deck+**
   - Within ~2 seconds, expect a `connected: Stream Deck +` log line
     followed by serial number, firmware version, key count, dial count,
     key image format, and touch strip format.
   - Expect all 8 keys to light up immediately, each showing a distinct
     background color and its own index number (0-7).
   - Expect the touch strip to show 4 labeled zones: `D0 BRIGHTNESS`,
     `D1 COUNTER`, `D2 COUNTER`, `D3 COUNTER`, each with a starting value.

3. **Key presses**
   - Press and hold any key: it should invert to a black background with
     white text while held, and log `key[N] PRESSED`.
   - Release it: it should return to its original color, and log
     `key[N] released`.
   - Try all 8 keys.

4. **Dial 0 (brightness)**
   - Rotate dial 0: the overall deck brightness should change live and
     smoothly, the `D0 BRIGHTNESS` zone should update to show the new
     percentage, and each tick should log `dial[0] TURN ... (clockwise` or
     `counter-clockwise)`.
   - Press dial 0: brightness resets to the default (75%), the zone
     updates, and it logs `dial[0] PRESSED` / `released`.

5. **Dials 1-3 (counters)**
   - Rotate each dial: its zone's counter value should update on the touch
     strip (only that zone repaints -- proving partial touch-strip
     updates), and it logs `dial[N] TURN ...`.
   - Press each dial: its counter resets to 0 (zone updates), logging
     `dial[N] PRESSED` / `released`.

6. **Touch strip -- short tap**
   - Tap anywhere on the touch strip: expect a vertical yellow marker line
     drawn at the tapped x position (full strip repaint), and a log line
     `touch SHORT tap at (x, y)`.

7. **Touch strip -- long press**
   - Press and hold a point on the touch strip: expect a log line
     `touch LONG press at (x, y)`. No visual change is expected for this one.

8. **Touch strip -- drag/swipe**
   - Swipe across the touch strip: expect a log line
     `touch DRAG from (x1, y1) to (x2, y2)`.

9. **Unplug while active**
   - Unplug the Stream Deck+ USB cable while the probe is running.
   - Expect a loud `Stream Deck+ disconnected` warning within a couple of
     seconds, the process should NOT crash, and it should return to the
     `waiting for Stream Deck+...` polling state.

10. **Replug**
    - Plug the Stream Deck+ back in.
    - Expect it to be detected automatically within ~2 seconds and fully
      repainted (step 2 again), with no restart of the process required.
    - Repeat steps 9-10 a few times to confirm the cycle is reliable.

11. **Ctrl+C shutdown**
    - With the device connected and active, press `Ctrl+C`.
    - Expect the deck to reset/blank, a `deck-probe shutting down` log
      line, and the process to exit with code 0.
    - Also try `Ctrl+C` while in the `waiting for Stream Deck+...` state
      (no device connected) -- same clean exit expected.

## Troubleshooting

**Device not found / stuck waiting forever**
- Is the official Elgato Stream Deck app running? Quit it -- it holds
  exclusive HID access and this probe cannot open the device while it's
  running.
- Is the USB cable a data cable (not charge-only) and firmly seated? Try a
  different cable or port.
- Confirm the OS sees the device at all: on macOS, `system_profiler
  SPUSBDataType | grep -A 5 Elgato` should show a "Stream Deck +" entry.

**`Could not find the native HIDAPI library...` error at startup**
- macOS: `brew install hidapi`, then retry.
- Debian/Ubuntu: `sudo apt install libhidapi-libusb0`, then retry.
- This error means the library is missing entirely -- it is not a
  permissions issue and won't resolve itself by retrying without installing
  the package.

**Permissions**
- macOS: no special permissions are needed for USB HID access with this
  library.
- Linux: you need a udev rule granting your user access to the Stream
  Deck+'s USB HID interface (VID `0fd9`). Create
  `/etc/udev/rules.d/50-elgato-streamdeck.rules` with:
  ```
  SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0666"
  SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", MODE="0666"
  ```
  Then reload rules and replug the device:
  ```sh
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  ```
