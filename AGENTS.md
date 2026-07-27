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

## Testing

- `uv run pytest` runs the whole suite (`tests/`) -- 240 tests, no
  hardware, no network, no real service, all in well under a second.
- `tests/conftest.py` carries safety rails that make it structurally hard
  for a careless test to touch anything real, applied PREVENTIVELY: this
  repo's sibling, muxplex, had its own suite SIGTERM a live server and
  overwrite a real `settings.json` in one day (see that repo's
  `AGENTS.md`/`tests/conftest.py`) -- muxplex-deck carries the same class
  of exposure (a real config file + secret federation key, a real
  systemd/launchd service, exclusive real HID access), just without an
  incident yet. Read `tests/conftest.py`'s module docstring before changing
  anything there; `tests/test_safety_rails.py` fails loudly if a rail is
  removed or weakened.

  | Rail | Stops | Bypass |
  |---|---|---|
  | `pytest_sessionstart` guard | Running the suite at all while a real muxplex-deck service is active | `MUXPLEX_DECK_TEST_ALLOW_LIVE_SERVICE=1` |
  | autouse `MUXPLEX_DECK_CONFIG` -> tmp | A test with no explicit `config_path` reading/writing the real `config.json` or `federation_key` | none (always on; explicit `config_path` still works normally) |
  | autouse systemd/launchd path redirect | A test writing/removing a real unit file (`~/.config/systemd/user/`) or plist (`~/Library/LaunchAgents/`) | none (always on) |
  | autouse `XDG_STATE_HOME` -> tmp | A test overwriting a real running sidecar's `status.json` | none (always on) |
  | autouse `subprocess.run` neutering | A test shelling out to a real `systemctl`/`launchctl`/`loginctl`/`openssl`/`git`/`uv`/`pip` | `@pytest.mark.allow_real_subprocess` |
  | autouse HID neutering | A test opening/enumerating a real Stream Deck via `device_real.RealDeviceManager` | `@pytest.mark.allow_real_hid` |
  | `test_safety_rails.py` | Silent removal or weakening of any rail above | n/a |

  The two bypass markers are registered in `pyproject.toml`
  (`[tool.pytest.ini_options] markers`) and are currently unused by any
  test in this repo -- every existing test that needs real-ish behavior
  fakes it explicitly instead (e.g. `test_cli_service.py`'s
  `recording_run` fixture, `test_cli_doctor.py`'s `_FakeManager`). Reach
  for a marker only when a test genuinely needs the real implementation;
  it must be visible in review, not a default.
  The two path-isolation rails (`MUXPLEX_DECK_CONFIG`, `XDG_STATE_HOME`)
  are set via environment variable rather than by stubbing the resolver
  functions directly, so they don't fight `test_config.py`'s and
  `test_statusfile.py`'s own dedicated tests of those functions' sudo-
  aware / `XDG_STATE_HOME`-aware fallback logic -- those tests set/delete
  the env var themselves, which simply overrides the autouse default.

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
- **The udev remediation must be gated on udev actually running.** `udevadm
  control` talks to `/run/udev/control`; when that socket is absent (WSL
  without systemd, containers) the reload fails with "No such file or
  directory" and rules never fire. A real WSL user followed the printed
  block exactly and lost ~40 minutes. `TAG+="uaccess"` is additionally
  inert without a logind seat, which WSL has none of -- hence the added
  `GROUP="plugdev"`. Branch on the capability (`usbnode.udev_is_live()` --
  `Path("/run/udev/control").exists()`), never on the platform name. This
  is why it also repairs plain-Linux containers, and won't rot when WSL
  eventually gains udev. **Never print a command that cannot work on the
  machine you are printing it to.** All WSL/udev/permission guidance text
  now lives in one place, `hidhelp.py` (`explain_environment`,
  `explain_open_failure`, `udev_guidance`) -- consumed by `cli.py`
  (`doctor`, `status`, `wsl attach`), `service.py`, `main.py`, and
  `init_wizard.py`. Don't duplicate a copy of this text in a new surface;
  import `hidhelp` instead. `usbnode.py` (sysfs facts) and `wsl.py`
  (usbipd-win facts; `attach()` is the ONE mutating function in the whole
  surface) are the two modules `hidhelp` composes.
- **The sidecar's open-failure branch must update the status file.**
  `main.py`'s `deck.open()` except-branch didn't call `reporter.update()`
  (unlike the `deck is None` branch right above it) -- so a stuck-open
  device left the status file frozen at stale values, and `muxplex-deck
  status` reported a *false* "Server: unreachable" even though the server
  was never contacted. Any new failure branch in the hotplug loop must
  call `reporter.update(...)`, and any WSL/permission diagnosis must be
  computed once per failure *episode* (`main._FailureEpisode`), not once
  per poll cycle -- `hidhelp.explain_open_failure()` can shell out to
  `usbipd.exe` on WSL, and doing that every 2 seconds forever is its own
  bug.
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
- `update` is source-aware (v0.4.1+): it reuses `_get_install_info()` --
  the same PEP 610 `direct_url.json` detection `doctor`'s install-source
  check already relies on -- to decide *what* to reinstall. A `pypi`
  install upgrades from PyPI (`uv tool install --force muxplex-deck` /
  pip fallback); a `git` (or `unknown`) install keeps reinstalling from
  `main` via `git+...`, exactly as before. An `editable` install is left
  untouched (manage it via git yourself). It also now has the
  version-already-current skip gate muxplex's own `upgrade()` has (a real
  PyPI release makes that gate meaningful); `--force` bypasses it. Do NOT
  let `update`/`doctor` silently move a user off a source they chose --
  see the "PyPI vs git install" incident below.
- `doctor`'s install-source check (`check_install_and_update`) treats
  `pypi` as a fully known source, not `unknown install source` -- it
  checks the published version via PyPI's JSON API and reports up to
  date / update available exactly like the `git` path does via
  `git ls-remote`. **Incident (2026-07):** `doctor` used to call a
  correctly-detected `pypi` install "unknown" and tell the user to run
  `update`; `update` unconditionally reinstalled from `git+...`, silently
  reverting a user who had deliberately migrated to the PyPI release back
  onto git. Both entry points must agree on every known source, and
  neither may recommend an action that undoes the user's install choice.
