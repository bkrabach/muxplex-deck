# muxplex-deck: Stream Deck+ hardware probe & muxplex sidecar

This repo has two apps, sharing one `uv` project:

- **`deck-probe`** -- a PoC/spike app that proves we can drive every feature
  of an Elgato Stream Deck+ (8 LCD keys, 4 push-dial encoders, 800x100
  touch strip) and capture every input it can produce, including clean
  hotplug (unplug/replug without restarting the process). Device I/O only,
  no server/network integration. See "Stream Deck+ hardware probe" below.
- **`muxplex-deck`** -- the actual product: a sidecar that shows your
  muxplex tmux sessions on the deck's 8 keys and switches sessions on key
  press. See "muxplex sidecar" below.

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

## Stream Deck+ hardware probe

### Running

```sh
uv run deck-probe
```

Or equivalently:

```sh
uv run python -m deck_probe
```

Press `Ctrl+C` to exit cleanly at any time -- the deck is reset (blanked)
and the device handle is closed before the process exits.

### Verification checklist

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

### Troubleshooting

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

## muxplex sidecar (`muxplex-deck`)

Shows your muxplex tmux sessions on the deck's 8 keys and switches the
active session on key press. Polls `GET /api/sessions` + `GET /api/state`
on the muxplex server every `poll_interval` seconds; repaints only when
something render-relevant changed (no flicker). Dials and touch-strip
gestures are logged only in v1 -- unassigned, pending interaction design.

### Config

Config is a JSON file at `~/.config/muxplex-deck/config.json` by default
(override with `--config <path>` or the `MUXPLEX_DECK_CONFIG` env var):

```json
{
  "server_url": "https://spark-1:8088",
  "key_file": "~/.config/muxplex-deck/federation_key",
  "poll_interval": 2.0
}
```

- `server_url` (required) -- the muxplex server's base URL, reached over
  Tailscale from the Mac.
- `key_file` (optional, defaults to `~/.config/muxplex-deck/federation_key`)
  -- path to a file containing the federation Bearer key, read fresh at
  startup and whitespace-stripped.
- `ca_file` (optional) -- path to a CA bundle for TLS verification. Python
  does **not** use the macOS Keychain trust store, so if the server's
  certificate was issued by muxplex's own local CA (`muxplex setup-tls`),
  point this at that CA file. Never omit verification instead -- see
  Troubleshooting below.
- `poll_interval` (optional, default `2.0`) -- seconds between session
  polls while active.

Any missing/invalid config or unreadable key file produces a clear,
actionable message on stderr and a non-zero exit -- there is no default
that silently skips auth or TLS verification.

### Getting the federation key onto the Mac

The muxplex server already has a federation key generated
(`~/.config/muxplex/federation_key` on spark-1, federation enabled). Copy
it over:

```sh
mkdir -p ~/.config/muxplex-deck
scp spark-1:.config/muxplex/federation_key ~/.config/muxplex-deck/federation_key
chmod 600 ~/.config/muxplex-deck/federation_key
```

Then create `~/.config/muxplex-deck/config.json` with your `server_url`
(the default `key_file` path above already matches, so you can omit it).

### Running

```sh
uv run muxplex-deck
```

Or with an explicit config path:

```sh
uv run muxplex-deck --config ~/some/other/config.json
```

`Ctrl+C` exits cleanly at any time -- the deck is reset (blanked) and the
device handle closed before the process exits.

### Verification checklist

1. **Cold start** -- run with the deck unplugged: waits quietly, no
   server traffic (check with e.g. `tcpdump` or just trust the code: the
   poll loop only starts once a device is open).
2. **Plug in, server reachable** -- keys populate with your first 8
   session names within `poll_interval`; the touch strip shows
   `<hostname> · N sessions · ACTIVE: <name>`.
3. **Bell indicator** -- trigger a bell in one of your tmux sessions
   (e.g. `printf '\a'`); its key should grow an amber dot within one poll
   tick.
4. **Press a key** -- switches muxplex's active session (confirm in the
   PWA or by reconnecting a terminal); the highlight (green background)
   moves to the pressed key's session on the next poll tick.
5. **Server down** -- stop muxplex (or block the URL) while the sidecar
   is running: strip shows `<hostname> UNREACHABLE -- retrying`, keys go
   blank, and it recovers automatically (repainting sessions) once the
   server comes back.
6. **Bad key** -- temporarily corrupt the key file: strip shows
   `AUTH FAILED -- check key file`, a `CRITICAL` log line appears, and it
   retries slowly (every 30s) rather than spinning.
7. **Unplug/replug** -- same hotplug behavior as the probe: unplug stops
   all server traffic and returns to waiting; replug brings up a fresh
   ACTIVE session.

### Troubleshooting

**SSL verification failure** (`SSLCertVerificationError` or similar)
- Your muxplex server is likely using a certificate from its own local CA
  (`muxplex setup-tls`) rather than a publicly trusted one. Set `ca_file`
  in your config to that CA's certificate path and retry.
- Never work around this by disabling verification -- point at the right
  CA file instead.

**401 / 403 -- `AUTH FAILED` on the strip**
- The federation key is missing, stale, or doesn't match the server's.
  Re-copy it from the server (see "Getting the federation key onto the
  Mac" above) and confirm `key_file` in your config points at it.

**Device not found / `Could not find the native HIDAPI library...`**
- Same as the probe -- see the Stream Deck+ hardware probe's
  Troubleshooting section above (quit the official Elgato app, install
  `hidapi`, check the USB cable).
