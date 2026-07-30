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
- **`KillMode=mixed` in `_SYSTEMD_UNIT_TEMPLATE` is safe here ONLY because the
  sidecar owns no long-lived children -- that is a load-bearing invariant, not
  an accident.** `mixed` SIGTERMs the main process and then **SIGKILLs every
  other process in the service cgroup**. On the sibling repo that directive
  destroyed 44 live tmux sessions in one `systemctl restart`, because muxplex
  auto-spawns a tmux server that inherits its cgroup (see `muxplex/AGENTS.md`,
  "Two ways to destroy every live tmux session on this host"). The sidecar has
  no equivalent exposure today: everything concurrent is an in-process
  `threading.Thread` (poll loop, optimistic connect, emulator HTTP server), and
  every `subprocess.run` in `cli.py`/`service.py`/`wsl.py`/`focus.py` is a
  short-lived, awaited command. `mixed` and `process` are therefore
  behaviourally identical for this unit. **If the sidecar ever gains a
  long-lived child -- a spawned helper, a detached usbip attach, a background
  daemon -- `mixed` becomes a mass kill and the template must change with it.**
  Do NOT copy muxplex's `KillMode=process` fix over pre-emptively: `mixed`'s
  SIGKILL escalation is what the `service stop` deck-reset work below is
  written against, and changing it would alter that path's assumptions for no
  present benefit.
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
- **Windows background service = Task Scheduler, at-logon, current user --
  never a Windows Service.** See docs/WINDOWS_NATIVE_SPEC.md section 1 for the
  full reasoning; the short version: creating/deleting a real Windows
  Service needs admin, and it runs as LocalSystem (wrong `%USERPROFILE%`)
  or a named account whose password would have to live in the SCM --
  both disqualifying before HID access is ever considered. `service.py`'s
  `_win_*` functions register a scheduled task via `schtasks /Create /XML`
  (`InteractiveToken` logon, `LeastPrivilege`, no stored password) whose
  action is `pythonw.exe -m muxplex_deck run --log-file <path>` with NO
  `cmd.exe` wrapper -- a wrapper would break the PID contract
  (`IRunningTask.EnginePID` must be the sidecar's own pid, not a
  wrapper's). `_win_task_query()` reads state via COM
  (`Schedule.Service`), never `schtasks /Query` text (localized, so
  unsafe to parse for a correctness-critical predicate). Honest
  trade-offs, surfaced in `service install`'s own output, not buried:
  starts at logon (not boot); worst-case restart latency ~60s (Task
  Scheduler's minimum repetition interval) vs systemd's ~5s.
- **Service command narration (install/uninstall/start/restart) goes
  through `report.py`'s VERDICT/STATE/ACTION renderer**, same as
  `doctor()`/`status()` (v0.7.1) -- not the old print-as-you-go
  `_step_ok`/`_step_warn` style. Each step becomes a `report.Check`
  collected into a list; ONE `report.render(...)` call prints the whole
  thing at the end. This is what let a Windows implementation (no
  `systemctl status`-shaped external command to lean on) present itself
  consistently with systemd/launchd. `service_stop()`/`service_logs()`
  are unchanged in kind -- `stop` was always silent, `logs` is (and stays)
  a raw passthrough stream; `service_status()` keeps macOS/Linux's raw
  `launchctl print`/`systemctl status` passthrough deliberately (more
  detail than we could reconstruct, display-only, never parsed for a
  decision) -- only Windows' `status` renders its own report, since
  `schtasks /Query /V` is explicitly rejected (localized, verbose).
- **A platform-dispatch gate that goes from provably-False to True is a
  live-bug trigger, not just a feature add.** `cli.update()`'s stop block
  hardcoded `launchctl`/`else: systemctl` -- latent only because
  `service_is_active()` returned `False` on Windows before Task Scheduler
  support existed. The moment `service_manager_available()`/
  `service_is_active()` can return `True` on a new platform, every branch
  that was guarded by "this platform can't get here anyway" needs a fresh
  look. Fixed by routing through `service.service_stop()` (one dispatch
  site) instead of a second, duplicated stop implementation in `cli.py`.
- **`IRunningTask.EnginePID` is NOT the sidecar's own pid -- VERIFIED FALSE
  on real hardware (2026-07), reversing docs/WINDOWS_NATIVE_SPEC.md section
  1.4's item 2.** A machine running exactly one healthy sidecar (its own
  log showed it start, connect the deck, poll the server, handle key
  presses) had `EnginePID` report a DIFFERENT pid than the one the sidecar
  itself wrote to `status.json` -- `service status` said "running (pid
  63268)", `status` said the published data was from "a previous run (pid
  20300)". Read only after the hardware disproved the assumption:
  Microsoft's own docs say `EnginePID` is "the process ID for the engine
  (process) which is running the task"
  (`learn.microsoft.com/windows/win32/taskschd/runningtask-enginepid`) --
  the Task Scheduler engine HOST process, not the task's own launched
  process, confirmed by independent reports since 2011. This holds even
  for a direct, unwrapped `pythonw.exe` action -- the no-`cmd.exe`-wrapper
  reasoning in section 1.2 was necessary but not sufficient. **Fix:**
  `service_main_pid()` now always returns `None` on Windows rather than
  fabricating an authoritative-looking wrong answer; `status()`'s existing
  "pid undeterminable -> fall back to age-based staleness" path (already
  built for exactly this "cannot determine" case) makes this honest
  automatically. The v0.5.3 restart contract (never claim success before
  the NEW process's status write is actually observed) is preserved via a
  DIFFERENT mechanism on Windows: `_win_wait_for_fresh_status()` compares
  the status file's recorded pid against a BASELINE captured before
  `_win_restart()` calls `_win_stop()`, rather than against a live-queried
  pid that Windows cannot reliably provide. Because `_win_stop()` hard-
  kills the old process before `_win_start()` launches a new one, any pid
  that appears afterward is necessarily a new process's write -- an
  equally reliable signal, sourced from the sidecar's own self-report
  (authoritative), never fabricated from `EnginePID`.
- **`schtasks /Create /RU <user>` without `/RP` hangs on stdin,
  interactively, EVERY time -- VERIFIED on real hardware (2026-07) and
  confirmed by Microsoft's own docs.** `service install` hung with zero
  output until the user pressed Enter, which then unblocked it instantly.
  Root cause: `_win_install()`'s `schtasks /Create /XML ... /RU <user> /F`
  passed `/RU` on the command line without `/RP` -- Microsoft's own
  reference page states plainly: *"Schtasks always prompts for a password
  unless you provide one, even when you schedule a task on the local
  computer using the current user account. This is normal behavior for
  schtasks."* This is unconditional -- it does not matter that the XML's
  `<Principals><Principal><LogonType>InteractiveToken</LogonType>` needs
  no password at all. `subprocess.run` inherits the parent's stdin by
  default, so a real terminal blocked silently on that prompt; pressing
  Enter submitted a blank password and unblocked it (task still got
  created, since InteractiveToken never uses the stored password). **Fix:
  drop `/RU` from the command line entirely** -- Microsoft's docs confirm
  `/XML` can be used alone when the file already contains the user account
  information, which ours does (the `<Principals>` block). Guessing an
  `/RP` value instead was rejected: an empty/blank password could get
  *stored* against the account, which is both unverifiable from here and
  antithetical to the whole point of using `InteractiveToken`. **Also
  added, defense-in-depth:** every Windows `subprocess.run` call in
  `service.py` (schtasks and powershell.exe alike) now passes
  `stdin=subprocess.DEVNULL` explicitly, so any FUTURE interactive prompt
  from either tool fails fast and loud instead of hanging silently on
  whatever stdin happens to be inherited.
- **`service restart` left the task registered but NOT running (state 3) --
  VERIFIED on real hardware (2026-07-28), root cause confirmed against
  documented `MultipleInstancesPolicy` semantics.** `muxplex-deck service
  restart` printed "has not published fresh status" and `status`
  afterward reported state 3 (`TASK_STATE_READY`) -- the user had to run
  `service start` manually every time. Root cause: `_win_restart()` called
  `schtasks /Run` immediately after `schtasks /End`, on the (wrong)
  assumption -- stated in the code's own comment -- that "Task Scheduler's
  /End has no separate unload race to wait out". `schtasks /End` requests
  termination but does not synchronously wait for Task Scheduler's own
  internal "is this task running" bookkeeping to catch up with the killed
  process, so `/Run` landed while the OLD instance was still considered
  running -- `MultipleInstancesPolicy=IgnoreNew` (chosen deliberately, see
  section 1.2) then silently discarded the new run request. Net effect:
  old process dies, new one never starts. **This was already the
  documented plan** -- docs/WINDOWS_NATIVE_SPEC.md section 1.6's `restart` row
  says "stop -> poll `state != RUNNING` (bounded, reusing
  `_wait_for_launchd_unload`'s shape) -> start" -- but the original
  implementation never actually added that poll. **Fix:**
  `_win_wait_for_task_stopped()` (service.py), polling `_win_task_query()`
  directly (never the cross-platform `service_is_active()` dispatcher --
  this function is already Windows-specific), gates `_win_restart()`'s
  `/Run` the same way `_wait_for_launchd_unload()` gates launchd's
  `bootstrap` after `bootout`. On timeout it reports honestly ("Task did
  not report stopped within Ns -- attempting restart anyway") and still
  attempts the restart, never silently giving up -- same contract as the
  launchd path. No documented Microsoft source states `/End` is
  asynchronous outright; this conclusion is inferred from (a) the
  real-hardware symptom being exactly what an async-teardown race would
  produce, (b) `IgnoreNew`'s documented behavior
  (learn.microsoft.com/windows/win32/taskschd/taskschedulerschema-multipleinstancespolicy-settingstype-element)
  confirming it silently drops a new run while an instance is considered
  running, and (c) the fix (poll until genuinely stopped, then run) fully
  resolving the reported symptom on the reporting user's own hardware.
- **Windows foreground focus: `AttachThreadInput` alone is NOT enough for a
  windowless background process -- VERIFIED on real hardware (2026-07-28),
  fixed with a second, independently-documented technique, no system-wide
  setting change.** The sidecar's log showed `SetForegroundWindow`
  "succeeding" while only flashing the taskbar icon, even with
  `AttachThreadInput` in place. Research (not hardware-verified, but
  Microsoft-documentation-grounded): `SetForegroundWindow`'s own docs list
  the exemptions that allow a foreground switch, and a StackOverflow report
  independently confirms `AttachThreadInput` specifically "doesn't work if
  your app is a background process without any windows and input focus" --
  exactly this sidecar's situation (no window, no message pump, never
  received input itself). **The fix, layered on top (kept, harmless):**
  `focus.py`'s `_raise_to_foreground()` now calls `tap_alt_key()` FIRST,
  unconditionally, on every focus attempt -- a `SendInput`-synthesized lone
  ALT keydown/keyup. This is not a brute-force hack: Microsoft's own
  `LockSetForegroundWindow` Remarks state plainly, "The system
  automatically enables calls to SetForegroundWindow if the user presses
  the ALT key or takes some action that causes the system itself to change
  the foreground window"
  (learn.microsoft.com/windows/win32/api/winuser/nf-winuser-locksetforegroundwindow).
  `SendInput` is the documented way to synthesize that keypress without a
  real keyboard. **Explicitly rejected:**
  `SystemParametersInfo(SPI_SETFOREGROUNDLOCKTIMEOUT, 0)`, the other
  commonly-cited workaround -- it persists a REGISTRY value
  (`HKCU\Control Panel\Desktop\ForegroundLockTimeout`) affecting every
  application on the machine, not just this sidecar, and was never going to
  be made opt-in silently; the SendInput approach needed no such
  compromise. **UNVERIFIED on real Windows hardware** whether the ALT-tap
  actually closes the gap the 2026-07-28 report found (the analysis above
  is Microsoft-doc-grounded, not yet hardware-confirmed) -- next real-deck
  session should confirm `focus_app` now raises the PWA window instead of
  only flashing its taskbar icon.
- **`service stop` left the deck showing its last-painted frame forever --
  VERIFIED on real Windows hardware (2026-07-28); fix implemented,
  screen-clear NOT yet hardware-confirmed (needs a real deck + a hard-killed
  sidecar to prove).** `service restart` now leaves the task running
  (previous fix), but a plain `muxplex-deck service stop` still left the
  Stream Deck's LCD keys showing whatever session icons were painted at the
  moment of the kill, indefinitely. Root cause, already documented in the
  v0.9.1 work above (`main._shutdown_cleanup()`'s own docstring): `schtasks
  /End` kills the sidecar via `TerminateProcess`, which bypasses the Python
  interpreter entirely -- no signal handler, no `finally`, so
  `_shutdown_cleanup()` never runs. In-process cleanup can never cover this
  path by construction; only a DIFFERENT process opening the now-free
  device afterward can. **Fix:** each platform's `service.py` stop function
  (`_win_stop`/`_systemd_stop`/`_launchd_stop`) now waits to CONFIRM the
  sidecar is genuinely gone -- reusing the same unload/stopped-poll each
  platform's `restart` already relies on (`_win_wait_for_task_stopped`,
  `_wait_for_launchd_unload`, `service_is_active()` after systemd's own
  blocking `stop`) -- then calls the new `_reset_deck_best_effort()`, which
  opens the device from the CLI process itself and reuses `main._safe_close`
  (one reset semantics, not two). Best-effort and non-fatal throughout: a
  missing deck, a deck still claimed by something else, or a hidapi load
  failure are reported but never turn a successful `stop` into a failure.
  **Not Windows-only on purpose:** systemd's `TimeoutStopSec=10` +
  `KillMode=mixed` and launchd's default `ExitTimeOut` both escalate to
  `SIGKILL` if the sidecar doesn't exit in time, which bypasses Python
  exactly like `TerminateProcess` does -- rarer than Windows (a sidecar that
  notices `shutting_down` promptly exits on its own SIGTERM first), but the
  same failure class, so the reset applies uniformly across all three
  platforms rather than special-casing Windows. `service stop` is
  consequently no longer silent (v0.9.3 and earlier): it now narrates
  whether the screen was actually cleared, through the same
  VERDICT/STATE/ACTION renderer `install`/`start`/`restart` already use.
  **UNVERIFIED:** whether the reset actually clears a real deck's screen
  after a genuine hard kill -- the logic is unit-tested (confirmed-stop
  gating, reset invocation, non-fatal device errors) but needs a real
  Stream Deck + Task Scheduler `service stop` to prove end-to-end.
