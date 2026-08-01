# muxplex-deck: Stream Deck+ hardware probe & muxplex sidecar

This repo has two apps, sharing one `uv` project:

- **`deck-probe`** -- a PoC/spike app that proves we can drive every feature
  of **any Elgato Stream Deck model** and capture every input it can
  produce, including clean hotplug (unplug/replug without restarting the
  process). It is capability-driven: on connect it prints a capability
  report (model, serial, firmware, key count/layout/size, dials, touch
  strip, touch keys) and exercises only the controls the connected deck
  actually has -- keys always, dials only if `dial_count() > 0`, the touch
  strip only if `is_touch()`. Verified on the Stream Deck+ (8 keys, 4
  dials, touch strip); the 15-key Original/MK2 (3x5, 72x72 keys, no
  dials/touch) runs the same probe with dial/touch exercises skipped.
  Device I/O only, no server/network integration. See "Stream Deck
  hardware probe" below.
- **`muxplex-deck`** -- the actual product: a sidecar that shows your
  muxplex tmux sessions on the deck's 8 keys and switches sessions on key
  press. See "muxplex sidecar" below.

`muxplex-deck` talks to the deck only through a small `DeckDevice` protocol
(`src/muxplex_deck/device.py`), with two interchangeable backends: the real
hardware (`device_real.py`, a thin wrapper over the `streamdeck` library)
and an in-process **emulator** (`emulator.py`) with a localhost web UI. This
means the whole sidecar -- state machine, muxplex client, rendering,
interaction flow -- can be developed and tested with zero hardware, on any
machine, with no `hidapi` installed. See "Stream Deck+ emulator" below.

## Quickstart (recommended)

```sh
uv tool install git+https://github.com/bkrabach/muxplex-deck
muxplex-deck init https://your-server:8088
muxplex-deck service install
```

`muxplex-deck init` is a turnkey setup wizard: it validates the server URL,
auto-fetches the server's CA certificate when one is needed (no `scp`/SSH
required -- and no risk of grabbing the *leaf* cert by mistake, a real
gotcha this project has hit before), walks you through pasting the
federation key, writes `config.json`, and re-verifies the server, CA, and
Stream Deck before handing off. It's idempotent -- re-run it anytime;
existing values become the defaults, nothing is clobbered.

**`uv sync` is for developing this repo, not installing the tool.** It only
builds `muxplex-deck`'s own `.venv` and does **not** put `muxplex-deck` on
your `PATH` -- this exact confusion has already cost real setup time. Use
`uv tool install` (above) to actually install and run the sidecar; reach
for `uv sync` only if you're hacking on muxplex-deck's source.

The manual/advanced setup (hand-editing `config.json`, `scp`-ing files
yourself) is still documented further down for anyone who wants it --
see "Service install walkthrough" and "muxplex sidecar" -> "Config" below.

## CLI

`muxplex-deck` has the same command shape as its sibling `muxplex` server
tool: a default action, a `config` group, a `service` group (systemd on
Linux, launchd on macOS), `doctor`, `update`, and `version`. Bare
`muxplex-deck` and `muxplex-deck run` do exactly the same thing.

| Command | What it does |
|---|---|
| `muxplex-deck` / `muxplex-deck run` | Run the sidecar (the default action) |
| `muxplex-deck --emulator` | Run against the in-process emulator instead of real hardware |
| `muxplex-deck config` / `config list` | Show all config keys and their current values |
| `muxplex-deck config get <key>` | Show one config value |
| `muxplex-deck config set <key> <value>` | Set a config value (type auto-detected) |
| `muxplex-deck config reset [key]` | Reset one key, or all keys, to defaults |
| `muxplex-deck service install` | Install + enable + start the background service |
| `muxplex-deck service uninstall` | Stop + disable + remove the service |
| `muxplex-deck service start` / `stop` / `restart` | Control the service |
| `muxplex-deck service status` | Show service status |
| `muxplex-deck service logs` | Tail service logs |
| `muxplex-deck status` / `status --json` | Show connected hardware, server reachability, and active session/view/page -- read from the running sidecar's published status, so it never contends with a running service for the (exclusive) HID handle |
| `muxplex-deck doctor` | Check Python version, install source, config, federation key permissions, `ca_file` validity, Stream Deck detection, HID openability, server reachability, and service status |
| `muxplex-deck update` (alias `upgrade`) | Update to the latest `main` and restart the service |
| `muxplex-deck version` / `--version` | Show the installed version |

Every `config` key maps 1:1 to a `config.json` field: `server_url`,
`key_file`, `ca_file`, `poll_interval`, `sort`, `focus_app`. See "Config"
under "muxplex sidecar" below for what each one means.

### The HID-permission caveat (why `service install` prints a udev block)

Unlike the muxplex *server* (a plain user process), the sidecar needs raw
USB HID access to the Stream Deck -- which a non-root Linux user does not
have by default. This is why you've been running `sudo muxplex-deck`. A
systemd **user** service, however, runs as your normal user, not root -- so
without a udev rule granting your user access to the device (vendor id
`0fd9`), the installed service will start but fail to open the deck.

`muxplex-deck service install` checks for an existing rule under
`/etc/udev/rules.d/` or `/usr/lib/udev/rules.d/` and, if none is found,
prints a copy-pasteable remediation block (the same rule shown in
"Permissions" under the hardware probe section above) instead of silently
installing a service that won't work. It never writes to `/etc` itself --
only detects and reports. This remediation is only ever printed when udev
is actually running (`/run/udev/control` present) -- on WSL without
systemd, or in a container, it never fires anyway, so `muxplex-deck`
detects that and prints a different, WSL/container-appropriate fix instead
(see `docs/WSL.md`). Run the printed `sudo tee ... && sudo udevadm
control --reload-rules && sudo udevadm trigger` commands, then unplug and
replug the deck (or `muxplex-deck wsl attach` under WSL) and `muxplex-deck
service restart`.

On Linux, `service install` also attempts `loginctl enable-linger
<you>` (best-effort, non-fatal) so the service keeps running after you log
out -- appropriate for a headless, always-on sidecar. `muxplex-deck doctor`
reports both the udev rule and HID-openable status.

### Service install walkthrough (Linux)

```sh
uv tool install git+https://github.com/bkrabach/muxplex-deck
muxplex-deck init https://<your-server>:8088   # sets server_url, ca_file, federation key
muxplex-deck doctor          # confirms config, key file, CA, and deck are all in order first
muxplex-deck service install # writes the systemd user unit, enables linger, warns about udev if needed
muxplex-deck service status
```

On macOS the same commands install a launchd agent instead (no udev/linger
step -- macOS needs no special HID permissions).

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

## Stream Deck hardware probe (any model)

### Running

```sh
uv run deck-probe
```

#### Windows + WSL

USB devices are invisible to WSL until Windows hands them over. Rather
than driving `usbipd`/udev rules by hand for this PoC probe, install and
use the actual product instead -- it handles this for you:

```sh
uv tool install muxplex-deck
muxplex-deck wsl attach     # finds the deck, attaches it, tells you what's left
muxplex-deck doctor         # diagnoses every remaining step, with real values
```

Neither needs the details -- but see `docs/WSL.md` if you want them (BUSID/
device-node churn, the `usbipd` vs `usbipd.exe` trap, durable vs per-attach
permission options).

**Close the Elgato Stream Deck app on Windows first** -- it holds
exclusive HID access. The probe prints this same guidance when it finds
no device.

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
   - Expect: no-device guidance (udev/usbipd/Elgato-app hints) followed by
     `waiting for a Stream Deck (polling every 2s)...` logged once,
     not repeated every 2 seconds (a "still waiting" heartbeat is fine
     roughly every 30s).

2. **Plug in the Stream Deck+**
   - Within ~2 seconds, expect a `deck capabilities:` report block with
     model, serial number, firmware version, key count/layout/size, dial count,
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

Shows your muxplex tmux sessions on the deck's 8 keys -- each key rendered
as a mini terminal preview of that session -- and switches the active
session on key press. Dial 0 cycles views; dial 1 pages within the current
view. Polls `GET /api/sessions` + `GET /api/state` + `GET /api/settings` on
the muxplex server every `poll_interval` seconds; repaints only the keys
(and strip) whose content actually changed (no flicker, no wasted JPEG
encodes for an idle session).

**View-following:** the deck mirrors whatever view is active in the muxplex
PWA. Keys show the sessions belonging to the server's current view
(`active_view` from `GET /api/state`) -- `all`, `hidden`, or a user-defined
view -- filtered the same way the PWA does. An unknown/deleted view name
shows honestly as zero sessions rather than silently falling back to `all`.
The needs-attention predicate is the exact one the PWA uses: a session
needs attention iff `unseen_count > 0` and either it has never been seen or
the most recent fire is newer than the last time it was seen
(`Bell.needs_attention` in `client.py`) -- so an old, acknowledged bell
doesn't keep glowing forever. See "Key previews" below for how this and the
active-session state are shown (colored borders, matched to muxplex's own
brand palette).

**Dial 0 -- view cycling:** turning steps through `["all"] + <your named
views> + ["hidden"]` (wraps at the ends); the strip immediately echoes the
candidate view name while turning, and after ~400ms of no further ticks it
commits via `PATCH /api/state` -- so a fast spin sends exactly one request,
not one per tick. `hidden` is a reserved pseudo-view exactly like `all` --
never a member of your named views, but reachable the same way -- and
switching to it shows only the sessions you've hidden in the PWA (a
property of the session, orthogonal to view membership; a session can be
both hidden and a member of a named view). `active_view` is *global*
server-side state (last writer wins across every device/tab watching the
server), so turning the dial changes what the PWA shows too, exactly like
switching views there would. **Pressing** opens the **view picker** (see
"Dial-press picker mode" below) instead of jumping straight to `all`.

**Dial 1 -- paging:** turning moves ±1 page within the current view (8
sessions/page, clamped at the first/last page -- no wrap). Purely local --
no server writes. The strip shows a `pN/M` indicator whenever a view has
more than one page. Paging resets to page 1 whenever the active view
changes (from either dial 0 or the PWA). **Pressing** opens the **page
picker** (see "Dial-press picker mode" below) instead of resetting to page 1.

**Dial-press picker mode:** pressing either dial hands the 8 keys over to
a chooser instead of performing an immediate action -- useful once there
are more views or pages than there are keys (e.g. 9 views, or 9 pages of
sessions). Pressing **dial 0** opens the **view picker**: each key shows
one view name (`["all"] + <your named views> + ["hidden"]`, same list dial
0 cycles through); tapping a key switches to that view (`PATCH /api/state`) and
returns to the normal session display. Pressing **dial 1** opens the
**page picker**: each key shows a page number for the current view; tapping
one jumps straight there. While a picker is open: turning its *own* dial
scrolls the window if there are more than 8 options (the strip shows e.g.
`VIEW PICKER -- tap to choose · 1-8/9`); pressing the *same* dial again
closes the picker with no change; pressing the *other* dial switches
straight to that dial's picker. The option matching today's actual active
view/page is marked with the same cyan border used for the active session,
so it's obvious what you'd be leaving. A session key's normal connect
behavior is suspended while a picker is open -- key taps select a picker
option, not a session, until you pick one or back out.

**Sort order -- `"sort"` config field (`"attention"` default, or
`"server"`):** in `"attention"` mode the view's sessions are reordered
before paging so the most urgent land on page 1: sessions needing
attention first (newest bell fire first), then the active session, then
everything else by most-recent bell fire (`bell.last_fired_at` descending;
sessions that have never belled sort last, preserving incoming order among
themselves). This tier deliberately does NOT use `last_activity_at` --
that timestamp derives from tmux `#{window_activity}` and bumps on any pane
output (spinners, redraws, status-line clocks), which reordered the grid on
essentially every poll cycle with no real event behind it; a bell only
fires on the actual agent-turn-completion signal, so ordering is stable
between bells. `"server"` mode disables all of this and shows exactly what
the PWA's own `sort_order` setting (`alphabetical` or manual/server order)
produces -- the pre-existing behavior.

**Key previews:** each occupied key renders a small monospace crop of the
session's live pane (bottom-left corner, ANSI colors stripped in this first
pass -- see `rendering.py`'s docstring for the honest fidelity tradeoff),
with the session name banner and status border(s) always layered on top so
identity stays legible regardless of what's scrolling underneath. Status is
shown as a colored border rather than a background fill or dot, using the
exact brand colors from muxplex's own frontend (`frontend/style.css`):
**cyan `#00D9F5`** for the active session, **amber `#F1A640`** for
needs-attention. A session that's both active and needs attention gets both
rings at once (amber outer, cyan inner) rather than losing one status to
the other.

### Config

Config is a JSON file at `~/.config/muxplex-deck/config.json` by default
(override with `--config <path>` or the `MUXPLEX_DECK_CONFIG` env var):

```json
{
  "server_url": "https://<your-server>:8088",
  "key_file": "~/.config/muxplex-deck/federation_key",
  "poll_interval": 2.0,
  "sort": "attention",
  "focus_app": "muxplex"
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
- `sort` (optional, default `"attention"`) -- `"attention"` reorders each
  view's sessions before paging (attention first, then active, then most
  recently active); `"server"` disables that reordering and shows exactly
  what the PWA's own `sort_order` setting produces. See "Dial 0 / Dial 1 /
  Sort order" above for the full behavior.
- `focus_app` (optional, default off) -- macOS app name of the locally
  installed muxplex PWA; see "Bringing the PWA to the foreground" below.
- `controls` (optional, default `{}`) -- per-control action overrides; see
  "Control mappings" below. Not settable via `config set` -- use
  `muxplex-deck controls set` instead.

### Control mappings

Every physical control (key, dial turn, dial push) resolves to a named
*action*. A fresh install configures nothing and behaves exactly as
before -- defaults are computed from the connected deck's capabilities,
never stored in `config.json`.

```
muxplex-deck controls actions          # the full catalog: what's available
muxplex-deck controls                  # the resolved table for this deck
muxplex-deck controls set key.4 view_picker
muxplex-deck controls unset key.4      # back to the default for this address
muxplex-deck controls reset            # delete every override
```

Addresses are capability-space coordinates, never model names: `key.N`,
`dial.N.turn`, `dial.N.push`. An action is either **momentary** (fires on
a press -- valid on `key.N`/`dial.N.push`) or **relative** (consumes dial
tick counts -- valid on `dial.N.turn` only); `none` (unassign) is valid on
any address. The 19-action catalog:

| Action | Kind | Does |
|---|---|---|
| `session` | momentary | Connect the session shown in this slot (the default) |
| `view_picker` | momentary | Open/close the paged view picker |
| `page_picker` | momentary | Open/close the page picker |
| `page_prev` / `page_next` | momentary | Page -1 / +1 (clamped) |
| `none` | momentary | Unassigned -- blank, ignored |
| `view_cycle` | relative | Debounced active-view change by dial ticks |
| `page_cycle` | relative | Local paging by dial ticks |
| `view_all` | momentary | Jump straight to the `all` view |
| `page_first` / `page_last` | momentary | Jump to the first/last page |
| `view_prev` / `view_next` | momentary | Step the view back/forward one |
| `focus_app` | momentary | Bring the muxplex PWA to the foreground |
| `refresh_now` | momentary | Poll the server immediately |
| `toggle_last` | momentary | Connect the previously-active session |
| `brightness_up` / `brightness_down` | momentary | Brightness +-10% (floor 10%, cap 100%) |
| `brightness_cycle` | relative | Brightness by dial ticks (floor 10%, cap 100%) |

Example -- reclaim the Stream Deck+'s otherwise-dead dials 2/3:

```json
{
  "controls": {
    "dial.2.turn": "page_cycle",
    "dial.2.push": "page_first",
    "dial.3.push": "view_all"
  }
}
```

A binding that doesn't apply to the *currently connected* deck (e.g. a
Deck+ config loaded against an Original) is never fatal -- it's reported
by `muxplex-deck controls`, `muxplex-deck doctor`, and `muxplex-deck
status`, and the sidecar starts normally with the rest of the plan intact
(the deck is hot-pluggable; a stricter config-time check would make the
sidecar unstartable whenever the "wrong" deck -- or none -- is plugged in).

Brightness set via `brightness_up`/`brightness_down`/`brightness_cycle` is
**session-local, never persisted**: the sidecar always asserts full
brightness on every bring-up (real hardware powers on dim), so a dimmed
value is never written to `config.json`.

### Bringing the PWA to the foreground (macOS)

If the muxplex PWA is installed as a standalone app on the same Mac the
sidecar runs on, set `focus_app` to its application name and every
key-press session switch will also bring that window to the foreground --
press a key, see the terminal you just switched to. It runs `open -a
"<focus_app>"` on a background thread: it never delays the switch itself,
and a focus failure (wrong name, app not installed) is logged as a warning
and otherwise ignored.

```json
{
  "server_url": "https://<your-server>:8088",
  "focus_app": "muxplex"
}
```

**Finding the app name:** it's the name macOS shows for the installed PWA
-- in the menu bar next to the Apple logo while it's frontmost, or under
its Dock icon. For a PWA installed via Chrome's "Install app" this is the
PWA's own title (Chrome puts the `.app` bundle under `~/Applications/Chrome
Apps.localized/`); for Safari's "Add to Dock" it's the name you gave it.
If the sidecar logs `focus: ... failed`, the name doesn't match -- check
what `open -a "<name>"` does in a terminal.

Scope, deliberately narrow: focus fires **only** on an explicit key-press
that changes the active session -- never on dial turns, view/page changes,
poll-driven repaints, or pressing the already-active session's key -- so
the window is never yanked forward while you're just browsing the deck.
macOS-only today (on other platforms a configured `focus_app` logs one
INFO notice and is ignored); a Windows implementation is planned.

Any missing/invalid config or unreadable key file produces a clear,
actionable message on stderr and a non-zero exit -- there is no default
that silently skips auth or TLS verification.

### Getting the federation key onto the Mac

**Easiest:** `muxplex-deck init` (see "Quickstart" above) prompts you to
paste the key directly -- no SSH access to the server required, and it's
never echoed back or logged.

**Manual method**, if you'd rather not use the wizard: the muxplex server
already has a federation key generated (`~/.config/muxplex/federation_key`
on the server, federation enabled). Copy it over:

```sh
mkdir -p ~/.config/muxplex-deck
scp <your-server>:.config/muxplex/federation_key ~/.config/muxplex-deck/federation_key
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
   `<hostname> · N sessions · ACTIVE: <name>`; the deck is at full
   brightness (100%) even if it powered on dim.
3. **Bell indicator** -- trigger a bell in one of your tmux sessions
   (e.g. `printf '\a'`); its key should grow an amber (`#F1A640`) border
   within one poll tick.
4. **Press a key** -- switches muxplex's active session (confirm in the
   PWA or by reconnecting a terminal). The cyan (`#00D9F5`) active border
   moves to the pressed key **immediately**, before the server round-trip
   completes -- it does not wait for the next poll tick, and a second key
   press is accepted right away rather than blocking on the first one's
   connect.
5. **Server down** -- stop muxplex (or block the URL) while the sidecar
   is running: strip shows `<hostname> UNREACHABLE -- retrying`, keys go
   blank, and it recovers automatically (repainting sessions) once the
   server comes back.
6. **Bad key** -- temporarily corrupt the key file: strip shows
   `AUTH FAILED -- check key file`, a `CRITICAL` log line appears, and it
   retries slowly (every 30s) rather than spinning.
7. **Unplug/replug** -- same hotplug behavior as the probe: unplug stops
   all server traffic and returns to waiting; replug brings up a fresh
   ACTIVE session (at full brightness again).
8. **Dial-press picker mode** -- press dial 0: keys switch to a list of
   view names (strip reads `VIEW PICKER -- tap to choose`); tap one to
   switch views and return to the session display. Press dial 1: keys
   switch to page numbers; tap one to jump there. Pressing the same dial
   again exits without changing anything; pressing the other dial while a
   picker is open switches straight to that dial's picker.

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

## Stream Deck+ emulator

`muxplex-deck` can run against an **in-process virtual Stream Deck+** instead
of real hardware -- no device, no `hidapi`, no native library at all. This
is the same code path as real hardware above the device layer: state
machine, muxplex client, polling, rendering, key-press-to-session-switch.
Only the physical HID transport is swapped out (see "How this works" below).

### Running

```sh
uv run muxplex-deck --emulator
```

Then open **http://127.0.0.1:8484** in a browser. You'll see the 8 keys and
the touch strip rendered as images, a Plug in / Unplug toggle, and simple
dial controls. The emulator starts **unplugged** -- click "Plug in" to bring
the sidecar's state machine up (same as physically connecting the real
device), point your `config.json` at a running muxplex server as usual, and
watch keys populate with session names. Click a key to switch sessions,
exactly like pressing a physical button.

Use a different port with `--emulator-port <N>` if 8484 is taken.

The same HTTP endpoints the UI polls (`GET /state`, `GET /keys/<n>.jpg`,
`GET /strip.jpg`, `POST /plug`, `POST /unplug`, `POST /input/key`,
`POST /input/dial`, `POST /input/touch`) can be driven directly with
`curl`/`httpx` -- useful for scripted or agent-driven testing without a
browser at all.

### What this proves vs. what only real hardware proves

**Proves (same code as production):** the hotplug state machine
(DEVICE_ABSENT/ACTIVE/UNREACHABLE/AUTH_FAILED transitions), the muxplex HTTP
client against a real muxplex server, session-list polling and repaint-only-
on-change logic, key-press-to-`connect_session` wiring, and all of
`rendering.py`'s image generation (via the same `PILHelper` calls, same
image formats/sizes).

**Does NOT prove:** anything about the physical USB/HID transport itself --
whether the real `streamdeck` library's read thread, hidapi bindings, or
actual device firmware behave as expected. That layer is covered by
`deck-probe` against real hardware (see above), not by the emulator. Always
confirm a change against real hardware before considering it fully proven.

### How this works

`muxplex_deck.main` depends only on the `DeckDevice` protocol
(`device.py`), never on the `streamdeck` library directly. `--emulator`
selects `emulator.EmulatorDeviceManager` instead of
`device_real.RealDeviceManager` as the one and only backend-selection point
(`main._build_manager`) -- the two backends are mutually exclusive imports,
so running with `--emulator` never imports (and thus never risks
constructing) anything hidapi-dependent.

The emulator's HTTP server runs for the whole sidecar process -- it *is*
the virtual USB bus. "Unplug" doesn't stop the server; it flips a flag so
the manager's `find_device()` returns nothing (exactly like a real
`DeviceManager.enumerate()` returning no devices), which drives the sidecar
into `DEVICE_ABSENT` with zero muxplex traffic, same as pulling a real
cable. "Plug" flips it back for a fresh `ACTIVE` bring-up.

### Plane mode / fully offline development

To develop with **no network at all** (e.g. no Tailscale reachability),
run a local muxplex server on the same machine as the sidecar:

1. **Before you lose connectivity**, install tmux and muxplex on the Mac.
   muxplex isn't assumed to be on PyPI -- install it from source/git, e.g.:
   ```sh
   brew install tmux
   git clone <muxplex-repo-url> ~/dev/muxplex
   cd ~/dev/muxplex && uv sync
   ```
2. Start a local muxplex bound to localhost (no TLS needed for localhost):
   ```sh
   uv run muxplex --host 127.0.0.1 --port 8099
   ```
3. Point the sidecar's config at it. Localhost muxplex still expects the
   `Authorization` header to be present and non-empty (check your muxplex
   version's auth requirements), so `key_file` is still required by
   `config.py` -- a dummy file is fine if your local instance doesn't
   enforce a real key:
   ```json
   {"server_url": "http://127.0.0.1:8099", "key_file": "~/.config/muxplex-deck/dummy_key"}
   ```
   ```sh
   mkdir -p ~/.config/muxplex-deck && echo "dummy" > ~/.config/muxplex-deck/dummy_key
   ```
4. Run the sidecar in emulator mode as above (`uv run muxplex-deck
   --emulator`) and click "Plug in". You now have a complete, fully offline
   dev loop: virtual deck -> sidecar -> local muxplex -> real tmux sessions
   -- no hardware, no network, no remote server required.
