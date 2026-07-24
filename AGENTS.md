# muxplex-deck — Conventions for Agents & Contributors

## What this is

A cross-platform Python sidecar that drives an Elgato Stream Deck+ (8 LCD
keys, 4 push-dials, touch strip) against a remote muxplex server via its
`/api/*` contract (`python-elgato-streamdeck` + `httpx`). It runs on the
machine the deck is plugged into (macOS today; Windows/Linux planned).
`src/deck_probe/` is the hardware-only PoC; `src/muxplex_deck/` is the
product. See `README.md` for setup, config, and verification checklists.

## The device seam — keep HID isolated and pluggable

- `device.py` defines the `DeckDevice`/`DeviceManager` protocols;
  `device_real.py` is the only hidapi-touching module; `emulator.py` is a
  virtual deck with a web UI. `main._build_manager` is the single
  backend-selection point — `--emulator` must never import hidapi code.
- Agents can drive the emulator headless: `muxplex-deck --emulator`, web UI
  at `http://127.0.0.1:8484`, endpoints `/state`, `/keys/N.jpg`,
  `/strip.jpg`, `/plug`, `/unplug`, `/input/key|dial|touch` — all curl-able.

## Real-hardware sign-off is mandatory

- The emulator is for headless/agent-driven iteration, **not** hardware
  sign-off — emulation missed the undocumented full-strip region-args
  requirement. Prove device I/O on the physical deck before calling it done.
- **Stream Deck gotcha**: `set_touchscreen_image` for the full strip
  REQUIRES explicit `(x, y, width, height)` region args — there are no
  defaults (`IndexError: Invalid draw width 0` otherwise).
- The hotplug/error recovery loop needs backoff, or it strobes the device.

## Hotplug state machine is core, not polish

- `DEVICE_ABSENT` (idle, zero server traffic) → `ACTIVE` →
  `SERVER_UNREACHABLE` (backoff, status shown ON the deck). Both plug and
  unplug are handled automatically — truly plug-and-play, no restarts.

## Optimistic repaint — never block the HID callback thread

- On key press: update local state + repaint the highlight IMMEDIATELY,
  then fire the server connect on a background thread; the next poll
  reconciles. This is why the deck feels instant. Mirrors how the muxplex
  PWA renders before awaiting its POST.

## Defer UI polish until the pipe is proven

- The deck itself is the status display for v1; menubar/tray apps come
  after the core integration works end-to-end. Iterate interaction design
  on real hardware once the walking skeleton is proven.

## Testing safety against the live server

- NEVER send write-verbs to the live muxplex (`:8088`). Use the
  scratch-instance pattern for anything mutating: isolated `HOME`,
  `TTYD_PORT` override (17682), `env -u TMUX`. The sidecar is read-heavy,
  but `connect` and `PATCH /api/state` are writes.
- The muxplex read model is a ~2s poll cache: create/delete aren't visible
  until the next cycle; wait ~3s after writes before asserting.

## Config

- `~/.config/muxplex-deck/config.json` (`--config` / `MUXPLEX_DECK_CONFIG`
  override): `server_url`, `key_file` (federation Bearer key), optional
  `ca_file`, `poll_interval`, `sort` (`"attention"` default | `"server"`),
  `focus_app` (macOS PWA foregrounding on key-press switches).
- Python httpx does NOT use macOS Keychain trust — if the server cert is
  from muxplex's local CA, `ca_file` must point at it. Never disable
  verification instead.
- Fail closed: missing/invalid config or key file is a clear stderr message
  and non-zero exit — no default silently skips auth or TLS.
