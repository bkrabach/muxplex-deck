# muxplex-deck — Conventions for Agents & Contributors

## What this is

A cross-platform Python sidecar that drives an Elgato Stream Deck+ (8 LCD
keys, 4 push-dials, touch strip) against a remote muxplex server via its
`/api/*` contract (`python-elgato-streamdeck` + `httpx`). It runs on the
machine the deck is plugged into (macOS today; Windows/Linux planned).
`src/deck_probe/` is the hardware-only PoC; `src/muxplex_deck/` is the
product. See `README.md` for setup, config, and verification checklists.

## Capability-driven, never model-name-driven

- Adapt to a deck by querying capabilities (`key_count()`, `key_layout()`,
  `key_image_format()['size']`, `dial_count()`, `is_touch()`,
  `touch_key_count()`, `is_visual()`) — NEVER by matching `deck_type()`
  strings (Original vs MK2 collide; new models would need matrix updates).
- Never assume key pixel size (Plus=120, Neo=96, Original/MK2=72) or that
  dials/touchscreen exist. Gate each control's paint + callback on its
  capability and log a "no X on this model" note when skipping.
- `deck_probe/capabilities.py` owns this: `describe_capabilities(deck)` is
  a pure dict-builder (testable with fakes, see `tests/`), plus
  `exercises_*` gating predicates and the report formatter.

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

## CLI (cli.py, service.py) -- parity with muxplex's own CLI

- `cli.py` is the console-script entry point (`muxplex-deck = "muxplex_deck.cli:main"`),
  ported from `muxplex/cli.py`'s shape: `_add_run_flags()` shared between the
  root parser and `run` subcommand (bare `muxplex-deck` == `muxplex-deck
  run`, all flags default to `None` for 3-tier CLI>config.json>default
  resolution), a `config` group backed by `config.py`'s `DEFAULT_CONFIG` +
  `load_raw_config`/`save_raw_config`/`patch_raw_config`, `doctor`, `update`
  (alias `upgrade`), and `version`. `main.py`'s own `main()` now just
  delegates to `cli.main()` -- argument parsing is single-sourced in
  `cli.py`, not duplicated.
- **The HID-permission caveat has no muxplex analog.** muxplex is a plain
  user process; the sidecar needs raw USB HID access a non-root Linux user
  doesn't have by default (why you've been running `sudo muxplex-deck`). A
  systemd **user** service runs as your normal user, not root -- so
  `service.py`'s `_systemd_install()` checks `/etc/udev/rules.d/` +
  `/usr/lib/udev/rules.d/` for a rule mentioning vendor id `0fd9` and prints
  a copy-pasteable remediation block (never writes to `/etc` itself) when
  none exists, rather than silently installing a service that can't open
  the device.
- **Restart policy differs from muxplex on purpose**: `Restart=always` (not
  muxplex's `on-failure`) plus a best-effort `loginctl enable-linger` on
  install -- this is a headless, always-on sidecar meant to survive logout,
  not a server a human restarts interactively.
- `doctor` additionally verifies `ca_file` is actually a CA (`openssl x509
  -noout -ext basicConstraints`, warns loudly on `CA:FALSE`) -- this is the
  exact real-world mistake (pointing `ca_file` at the server's *leaf* cert,
  `muxplex.crt`, instead of its CA, `ca/muxplex-ca.crt`) that cost real
  debugging time earlier in this project. It also probes the Stream Deck
  via the real `DeviceManager` and reports detected/openable status
  separately, since "detected but can't open" is exactly the
  udev-rule-missing symptom.
- `update` is git-only (no PyPI release) -- always reinstalls from `main`
  via `uv tool install --force git+...` (pip fallback), unlike muxplex's
  own upgrade which has PyPI/uv-tool-managed detection and a
  version-already-current skip gate. Simplified deliberately: there's only
  one install path for this project today.
