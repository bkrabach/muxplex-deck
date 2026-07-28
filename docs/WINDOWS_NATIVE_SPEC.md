# muxplex-deck — Native Windows Support: Implementation Specification

**Target repo:** `muxplex-deck/` (main, clean, v0.5.3, on PyPI)
**Status:** design only — no implementation written
**Author context:** written from a Linux host. **I cannot execute anything on Windows.**
Every claim below is either (a) verified by reading source on this machine, (b) verified
from an authoritative published document, or (c) **explicitly flagged as unverified** in
§10. Nothing is asserted as tested that was not tested.

---

## 0. What the spike already settled (do not re-litigate)

Verified on real hardware today, Windows 11 / native CPython 3.14, Elgato app closed,
deck detached from WSL:

```
found 1
Stream Deck Original 15
OK
```

- `streamdeck` 0.9.8 and `pillow` 12.3.0 install cleanly as cp314 wheels.
- The device layer opens, enumerates, and reports capabilities **with no code change**.
- The only blocker was a missing HIDAPI shared library, resolved by dropping
  `hidapi.dll` (x64) into the working directory.

So this port is **not** a device-layer port. It is three things: a service story, a DLL
packaging story, and a guidance story. In that order of difficulty.

---

## 1. Decision 1 — The background service on Windows

### 1.1 The session-0 question, answered honestly

The brief asked me to verify whether a true Windows Service's session-0 isolation blocks
USB HID access. **I could not verify this** — it needs a Windows box and a service to
test with, and I have neither.

Best-evidence position, for the record so nobody re-opens it: session 0 isolation governs
**window stations and desktops** (UI), not device objects. HID device interfaces are
global names in the object manager, not session-scoped, and hidapi's Windows backend
reaches them through SetupAPI enumeration plus `CreateFile` on the device-interface path
— no window handle, no `RegisterRawInputDevices`, no message pump. On that reading a
session-0 service *should* be able to open a Stream Deck.

**But the question is moot**, because three other properties of Windows Services rule
them out before HID access is ever reached. Each is independently disqualifying:

| # | Fact | Why it disqualifies |
|---|---|---|
| S1 | Creating or deleting a service requires `SC_MANAGER_CREATE_SERVICE` — i.e. **administrator**. | `muxplex-deck service install` becomes an admin-only command. The non-negotiables say a step that needs admin gets *printed*, not run. That turns the single most important onboarding command into a copy-paste ceremony — the exact WSL failure mode this whole effort exists to avoid. |
| S2 | A service runs as **LocalSystem** (whose `%USERPROFILE%` is `C:\Windows\System32\config\systemprofile`) or as a named account **whose password must be stored in the SCM**. | Config lives at `~/.config/muxplex-deck/config.json` and the federation key is a user-owned secret. LocalSystem resolves `~` to the wrong place; the named-account path means asking a user for their Windows password and handing it to the SCM. Neither is acceptable. |
| S3 | Python cannot be a service directly. It needs `pywin32`'s `pythonservice.exe` (a new dependency plus a `SERVICE_CONTROL_STOP` handler) or a redistributed third-party wrapper (WinSW, NSSM). | New dependency or new redistributed binary, and S1 still applies on top. |

**S2 alone is fatal**, and it does not depend on any Windows-specific behavior I cannot
test. Record it in `AGENTS.md` so the next session doesn't spend a day on session 0.

### 1.2 Decision: Windows Task Scheduler, at-logon, interactive user

Register a scheduled task in the **current user's** context via `schtasks.exe /Create /XML`.

| Property | Value | Why |
|---|---|---|
| Task name | `\muxplex-deck` (root folder) | Matches the systemd unit / launchd label naming. No folder to create. |
| `<LogonType>` | `InteractiveToken` | No stored password. Runs as the logged-on user → `~` resolves correctly, the 0600 key is readable, `uv`'s shims are on PATH. |
| `<RunLevel>` | `LeastPrivilege` | Never elevates. Registration in one's own context needs no admin. |
| Trigger | `<LogonTrigger>` scoped to `<UserId>` = the current user | Starts when this user logs on. |
| Restart | `<Repetition><Interval>PT1M</Interval></Repetition>` on the logon trigger + `<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>` | One mechanism, complete coverage. See §1.3. |
| `<ExecutionTimeLimit>` | `PT0S` (no limit) | **Critical.** The default is 3 days, after which Task Scheduler kills the task. A sidecar that silently dies after 72h is worse than one that never starts. |
| `<DisallowStartIfOnBatteries>` / `<StopIfGoingOnBatteries>` | `false` / `false` | **Critical on laptops.** Both default to `true` — the sidecar would refuse to start, or be stopped mid-session, on battery. |
| `<RunOnlyIfIdle>` / `<StopOnIdleEnd>` | `false` / `false` | Defaults would stop the task when the machine leaves idle. |
| `<StartWhenAvailable>` | `true` | Recovers a trigger missed while the machine was asleep. |
| `<AllowStartOnDemand>` | `true` | Required for `schtasks /Run` (the `service start` verb). |
| `<Hidden>` / `<WakeToRun>` | `false` / `false` | Discoverable in the Task Scheduler UI; never wakes the machine. |

**Action — this is load-bearing, do not "simplify" it:**

```xml
<Exec>
  <Command>C:\...\pythonw.exe</Command>
  <Arguments>-m muxplex_deck run --log-file "C:\Users\&lt;u&gt;\.local\state\muxplex-deck\muxplex-deck.log"</Arguments>
</Exec>
```

Three constraints on that action, each with a reason:

1. **`pythonw.exe`, not `muxplex-deck.exe`.** The console-script shim is a console
   subsystem binary; a task running it as the logged-on user shows a console window that
   sits on the desktop for the life of the sidecar. `pythonw.exe` is GUI-subsystem — no
   window. Resolve it as `Path(sys.executable).with_name("pythonw.exe")`.
   *(Fallback if absent: use `sys.executable`, and `_step_warn` that a console window will
   appear. Never fail install over this.)*
2. **No `cmd.exe /c` wrapper, no `.bat`, no redirection.** A wrapper would (a) show a
   console window and (b) break the PID contract — Task Scheduler's `EnginePID` reports
   the process *it* launched, which would be the wrapper, not the sidecar. This is why
   logging goes to a file from inside Python (§1.5) instead of via shell redirection.
3. **`--log-file` passed explicitly**, because Task Scheduler XML has no environment-
   variable element and `pythonw.exe` gives us `sys.stdout is None`.

### 1.3 Restart-on-failure: one mechanism, not two

Task Scheduler offers `<RestartOnFailure>`, but it only fires on a **non-zero exit** — it
does not cover a hung process, a hard kill, or a clean-but-wrong exit. The
repetition-trigger pattern (`PT1M` + `IgnoreNew`) covers **every** death mode with one
setting: if the task is running, the repeat is ignored; if it is not, it starts.

Use the repetition trigger. Do **not** also add `<RestartOnFailure>` — it is a second
mechanism with strictly narrower coverage, and complexity that adds no case.

**Honest trade to document:** worst-case restart latency on Windows is **60 seconds**
(Task Scheduler's minimum repetition interval) versus systemd's `RestartSec=5s`. State
this in `AGENTS.md` and in `service install`'s narration.

### 1.4 The PID contract — the thing v0.5.3 just fixed, preserved

`service._wait_for_fresh_status()` (service.py:583) polls until `status.json`'s recorded
`pid` equals the service manager's live PID. That contract must hold on Windows or the
restart-race fix regresses on the newest platform.

**Answer:** Task Scheduler exposes the PID of the process it launched. `IRegisteredTask.
GetInstances(0)` returns `IRunningTask` objects with an `EnginePID` property. Because the
task action is `pythonw.exe -m muxplex_deck run` directly (§1.2 constraint 2),
`EnginePID` **is** the sidecar's own PID.

Implement **one** PowerShell/COM query that answers all three service predicates at once —
this is both cheaper and locale-proof:

```
_win_task_query() -> WinTaskInfo(exists: bool, state: int | None, pid: int | None)
```

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "
  $s = New-Object -ComObject Schedule.Service; $s.Connect()
  try { $t = $s.GetFolder('\').GetTask('muxplex-deck') } catch { 'MISSING'; exit }
  $p = 0; foreach ($i in $t.GetInstances(0)) { $p = $i.EnginePID }
  'OK ' + $t.State + ' ' + $p
"
```

- `service_is_installed()` → `exists`
- `service_is_active()` → `state == 4` (`TASK_STATE_RUNNING`)
- `service_main_pid()` → `pid or None`

**Why COM and not `schtasks /Query /FO LIST`:** `schtasks` output is **localized** —
`Status:` and `Running` are translated on a non-English Windows. `State` is an integer.
Never parse localized console output for a correctness-critical predicate.

**Why not `Get-ScheduledTask`:** it returns state but not PID. One query beats two.

**Cost:** ~200–400 ms per PowerShell spawn. `_wait_for_fresh_status()` polls at 0.2 s /
5 s on POSIX; on Windows use **0.5 s interval, 10 s timeout** (20 polls). Make both
constants platform-conditional at module scope in `service.py`; the polling function
itself is unchanged.

The parse function (`_parse_win_task_query(stdout) -> WinTaskInfo`) must be **pure** and
never raise, matching `service_main_pid()`'s existing never-raise contract: any
unparseable output reads as "cannot determine."

### 1.5 Logging and `service logs`

`pythonw.exe` leaves `sys.stdout` and `sys.stderr` as `None`. `logging.basicConfig()`
would build a `StreamHandler` around `None`.

Add `--log-file PATH` to `cli._add_run_flags()` (**all platforms**, default `None` = today's
behavior exactly — this is what keeps macOS/Linux at zero regression risk), and change
`main._configure_logging(log_file: Path | None)`:

- `log_file` given → `handlers=[RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")]`
- else if `sys.stderr is None` → `handlers=[logging.NullHandler()]` (defensive; a
  console-less launch must not crash)
- else → today's behavior, byte for byte

Default Windows log path: `<status_dir>/muxplex-deck.log`, i.e. alongside `status.json`
(`statusfile.default_status_dir()`). One state directory, not two.

`service logs` on Windows → `powershell.exe -NoProfile -Command "Get-Content -LiteralPath
'<log>' -Tail 50 -Wait"`, wrapped in the same `except KeyboardInterrupt: pass` the other
two platforms use.

### 1.6 Per-verb implementation

| Verb | Windows implementation | Notes |
|---|---|---|
| `install` | Write task XML → `schtasks /Create /TN muxplex-deck /XML <file> /F` | Gated on `_config_ready()` **exactly as systemd/launchd are** (service.py:496) — never register a task whose config guarantees a crash loop, because `IgnoreNew` + `PT1M` would relaunch it forever. Narrate each step in the existing ✓/! style. |
| `uninstall` | `schtasks /End /TN muxplex-deck` (ignore failure) → `schtasks /Delete /TN muxplex-deck /F` | Report "task was not registered" rather than a traceback when absent. |
| `start` | `schtasks /Run /TN muxplex-deck` | Idempotent-ish: with `IgnoreNew`, running it while active is a benign no-op. Report exit ≠ 0 through the existing `_report_*_failure` shape. |
| `stop` | `schtasks /End /TN muxplex-deck` | Hard-stop; see §1.7. |
| `restart` | `stop` → poll `state != RUNNING` (bounded, reusing `_wait_for_launchd_unload`'s shape) → `start` → `_report_restart_result(...)` **unchanged** | `_report_restart_result` is already platform-agnostic and calls `_wait_for_fresh_status()`. This is the whole point of §1.4. |
| `status` | Print the `_win_task_query()` result + XML path + log path in ✓/! style | No `systemctl status` analogue worth shelling to; `schtasks /Query /V` is localized and verbose. |
| `logs` | §1.5 | |

**Dispatch shape.** Each of the seven public wrappers (service.py:876–943) gains exactly
one arm, placed **second** so platform identity beats tool-presence probing:

```python
if _is_darwin():      _launchd_x()
elif _is_windows():   _win_x()          # sys.platform == "win32"
elif _have_systemctl(): _systemd_x()
else:                 _unsupported_platform_error("x")
```

Today on Windows all three predicates fail and every verb prints
`_unsupported_platform_error` — so this is purely additive; no existing branch changes.

**No `enable-linger` analogue.** Windows has no equivalent that doesn't require admin or
a stored password. `_enable_linger()` stays POSIX-only.

### 1.7 Two honest behavioral differences to document, not fight

1. **Starts at logon, not at boot.** Boot-start needs SYSTEM (admin) or stored
   credentials. Both rejected. This is arguably *correct* semantics for a desktop
   peripheral — nobody presses a Stream Deck key while logged out. `service install`
   should say so plainly, and print (never run) the escalation for anyone who genuinely
   wants boot-start:
   ```
   The sidecar starts when you log on, not at boot. Boot-start requires either
   administrator rights or storing your Windows password — muxplex-deck will not
   do either for you. If you want it, in an ADMIN PowerShell:
       schtasks /Change /TN muxplex-deck /RU SYSTEM
   (Note: as SYSTEM it will no longer find your config in C:\Users\<you>.)
   ```
   That last line matters — the escalation is only honest if it names its own cost.
2. **`schtasks /End` is a hard stop.** Windows does not deliver `SIGTERM` from
   `TerminateProcess`, so `main._install_signal_handler()` (main.py:1027) never fires and
   the deck is **not** blanked on `service stop` — it keeps its last frame. `signal.signal
   (SIGTERM, ...)` is legal on Windows and does not raise, so **no code change is needed**;
   this is a documentation item only. The sidecar already asserts brightness and repaints
   on every bring-up, so the next start recovers cleanly.

---

## 2. Decision 2 — `hidapi.dll` packaging

### 2.1 What the loader actually does (verified by reading source on this machine)

`StreamDeck/Transport/LibUSBHIDAPI.py:154` and `:120–86`:

```python
search_library_names = {"Windows": ["hidapi.dll", "libhidapi-0.dll", "./hidapi.dll"], ...}
for lib_name in library_search_list:
    library_name_no_extension = os.path.basename(os.path.splitext(lib_name)[0])
    found_lib = ctypes.util.find_library(library_name_no_extension)   # <-- FIRST
    ...
    HIDAPI_INSTANCE = ctypes.cdll.LoadLibrary(found_lib if found_lib else lib_name)
```

And CPython, verified locally:

- `ctypes/util.py`, `nt` branch: **`find_library(name)` on Windows searches `%PATH%` and
  nothing else** — it walks `os.environ['PATH']`, appending `.dll`, and returns the first
  file that exists.
- `ctypes/__init__.py`, `CDLL.__init__` with `winmode=None`: loads with
  `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS` (application dir + System32 + directories added via
  `AddDllDirectory`), and **only** adds `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR` when the name
  contains a separator. **`%PATH%` is not in `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`.**

Two consequences that drive the design:

- `os.add_dll_directory()` works for the `find_library`-returned-`None` branch.
- `%PATH%` works for the `find_library` branch — **and that branch runs first**. So a
  stray or wrong-architecture `hidapi.dll` earlier on `%PATH%` wins over anything we
  register, `LoadLibrary` fails, the bare `except: pass` swallows it, and the two
  remaining probe names resolve to the same bad DLL or nothing. **Our bundled DLL is then
  never tried at all.** This failure mode is silent and must be designed against.

### 2.2 Options, and what I rejected

| Option | Verdict |
|---|---|
| Depend on a PyPI package that ships it | **Rejected.** `hidapi` on PyPI builds a Cython extension (`hid.*.pyd`) — not a shared library loadable by a ctypes-by-name probe. `hid` is ctypes bindings that require the library separately. streamdeck's own docs tell Windows users to download `hidapi.dll` by hand, which is the strongest available evidence that no package satisfies this probe. *Caveat: I did not exhaustively audit PyPI; I am certain only about `hidapi` and `hid`.* |
| Detect and guide | **Rejected.** Violates the non-negotiable ("must not require the user to hand-place files") and reproduces exactly the manual-download friction that made the WSL path painful. Kept only as the arm64 / source-checkout **fallback**. |
| **Bundle the DLL** | **Recommended.** |

### 2.3 Licensing — cleared

HIDAPI is tri-licensed (fetched from `libusb/hidapi` today):

- `LICENSE.txt` — "HIDAPI can be used under one of three licenses… The license chosen is
  at the discretion of the user of HIDAPI."
- `LICENSE-bsd.txt` — 3-clause BSD. Binary redistribution obligation: *"Redistributions in
  binary form must reproduce the above copyright notice, this list of conditions and the
  following disclaimer in the documentation and/or other materials provided with the
  distribution."*
- `LICENSE-orig.txt` — even more liberal: *"This software may be used by anyone for any
  reason so long as the copyright notice in the source files remains intact."*

**Take the BSD license.** It is the conventional choice for binary redistribution, its one
obligation is trivially satisfiable, and — critically — **it does not touch muxplex-deck's
own license**, which the GPLv3 option would.

Compliance, concretely:
1. Ship `src/muxplex_deck/_vendor/hidapi/LICENSE-bsd.txt` verbatim inside the wheel,
   next to the DLL.
2. Add a short "Third-party components" section to `README.md` naming HIDAPI, its
   copyright (`Copyright (c) 2010, Alan Ott, Signal 11 Software`), the BSD terms, and the
   upstream URL.

> **Open item for the maintainer:** `muxplex-deck` has **no `LICENSE` file and no
> `license` field in `pyproject.toml`**. Redistributing a third-party binary under a named
> license inside an unlicensed package is a loose end. Decide the project's own license
> before the first Windows wheel ships. This is a decision for you, not for me.

### 2.4 Sourcing and pinning

Verified today via the GitHub API: the latest `libusb/hidapi` release is **`hidapi-0.15.0`**,
and it publishes exactly one asset, **`hidapi-win.zip`** (1,436,775 bytes), whose release
notes state it contains pre-compiled `hidapi.dll` + `hidapi.lib` **for x86 and x64**, plus
headers.

- **Vendor the binary into the repo** at `src/muxplex_deck/_vendor/hidapi/x64/hidapi.dll`.
- Alongside it: `LICENSE-bsd.txt` and a `VERSION` file recording the upstream tag and the
  **SHA-256 of `hidapi-win.zip`** it was extracted from.
- **Do not download at build time.** A CI fetch without a pinned digest makes every wheel a
  supply-chain roll of the dice; a CI fetch *with* a digest is just a slower version of
  vendoring. Vendor it, review the bump like any other dependency change.
- **x64 only.** Windows arm64 has no official prebuilt in that zip → detect-and-guide there
  (§4, `WIN-NOHIDAPI`). 32-bit Python is out of scope.

### 2.5 Wheel shape — recommendation: single wheel, DLL as package data

Two ways to ship it:

| | Cost |
|---|---|
| Platform-tagged `win_amd64` wheel | A build matrix, wheel-tag plumbing through `uv_build`, a publish workflow that produces N artifacts, and a new failure class ("PyPI has a wheel for the wrong tag"). |
| **One wheel, DLL as package data** | ~100 KB of dead weight for macOS/Linux users. |

**Recommend the single wheel.** The publish workflow is currently a five-line `uv build`
on `ubuntu-latest`; keeping it that way is worth far more than 100 KB. The load path is
guarded on `sys.platform == "win32"`, so the file is inert everywhere else. Revisit only if
size becomes a real complaint.

Requires confirming `[tool.uv.build-backend]` includes non-`.py` files under `module-root`
(§10.8).

### 2.6 Load mechanism — new module `muxplex_deck/hidapi_win.py`

Small, single-purpose, ~40 lines:

```
ensure_hidapi() -> Path | None
    # no-op returning None on non-win32
    # returns the directory it registered, or None if the vendored DLL is absent
```

Behavior on `win32`:
1. Resolve `Path(__file__).parent / "_vendor" / "hidapi" / "x64" / "hidapi.dll"`.
2. If absent → return `None` (source checkout without the blob, or arm64). Caller falls
   back to guidance; never raise.
3. `os.add_dll_directory(str(dll_dir))` — **keep the returned cookie in a module-level
   global.** Closing it (or letting it be GC'd) un-registers the directory.
4. **`os.environ["PATH"] = f"{dll_dir}{os.pathsep}" + os.environ.get("PATH", "")`.**

Step 4 is not redundant with step 3 — it is the fix for §2.1's silent-shadowing failure.
`find_library` runs first and reads only `%PATH%`; prepending makes it deterministically
resolve **ours**. Step 3 covers the other branch (`find_library` → `None` →
`LoadLibrary("hidapi.dll")` under `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`, which honors
`AddDllDirectory` but not `%PATH%`) and also lets dependent DLLs beside `hidapi.dll`
resolve. Both branches, deterministically ours. Idempotent — safe to call repeatedly.

**Call sites** (both are the single construction point in their module — no scattering):
- first statement of `device_real.RealDeviceManager.__init__` (device_real.py:149), before
  `_LibDeviceManager()`
- `deck_probe/main.py`, before its `DeviceManager()` at `main.py:150`

**Also fix:** `device_real._MISSING_HIDAPI_MESSAGE` (device_real.py:32) currently says
Windows hidapi is *"bundled with the 'streamdeck' wheel"*. That is **false** and it is what
a Windows user sees on failure. Replace the Windows line with the real state of the world
(bundled by us; if you're seeing this, the vendored DLL is missing or a different
`hidapi.dll` on `%PATH%` shadowed it; here is where to get one).

**Diagnosability:** after `ensure_hidapi()`, `doctor` runs `ctypes.util.find_library("hidapi")`
and prints the resolved path. If it is not our vendored path, that is the `WIN-DLL-SHADOW`
guidance in §4. A design that creates a failure mode owes the user a way to see it.

---

## 3. Decision 3 — Config location

### 3.1 Keep `~/.config/muxplex-deck/`. Do not honor `%APPDATA%`.

On Windows `Path.home()` is `%USERPROFILE%`, so the path becomes
`C:\Users\<u>\.config\muxplex-deck\config.json`. `config._expand()` (config.py:69) already
handles both `~/` and `~\`, so **this works today with no change**.

Rationale:
- One documented location across all four platforms. Every error string, README line, and
  `doctor` output that prints a path stays true everywhere.
- `%APPDATA%` would fork the story four ways and buy nothing functional.
- Dotfile directories under `%USERPROFILE%` are now normal on Windows (`.gitconfig`,
  `.ssh`, `.aws`, `.cargo`).
- `MUXPLEX_DECK_CONFIG` already exists for anyone who disagrees.

Same reasoning for the status file: `statusfile.default_status_dir()` →
`C:\Users\<u>\.local\state\muxplex-deck\`. Keep it. `os.chmod(0o700)` there is a near-no-op
on Windows — harmless, leave it.

### 3.2 WSL and native Windows get **independent** configs. That is correct, not a gap.

They do not collide by construction: WSL's `$HOME` is `/home/<u>` inside the distro's own
ext4; native Windows' is `C:\Users\<u>`. Do **not** add a bridge, for three reasons:

1. **Only one of them can hold the deck at a time** anyway — that is what the `usbipd`
   handoff means. They are two installs of a client, not one install with two faces.
2. **Sharing through `/mnt/c` loses POSIX mode bits.** `key_file.chmod(0o600)`
   (init_wizard.py:290) would silently become a no-op, and `check_federation_key()`'s
   `mode & 0o077` warning would fire forever on a file it cannot fix.
3. **Two writers, one file.** A systemd user unit and a scheduled task could both write
   `status.json` concurrently. There is no upside worth that.

Run `muxplex-deck init` once in each environment. `init` self-serves the CA from
`/api/ca` (init_wizard.py:148) and validates the key against the server before writing
(init_wizard.py:270) — **there is nothing to copy across.** Say that loudly in the docs;
it is the single fact that removes most of the original setup pain.

### 3.3 One required change: POSIX modes are not enforced on Windows

`cli.check_federation_key()` (cli.py:330) warns when `mode & 0o077`. On Windows
`Path.chmod()` only toggles the read-only bit — it does **not** restrict other users — so
the stat reads `0o666` and `doctor` warns on every single run, recommending a `chmod` that
cannot fix it.

That is a direct violation of the repo's own rule: *"Never print a command that cannot work
on the machine you are printing it to"* (AGENTS.md). Add a `win32` branch that reports
presence only:

```
Federation key: <path> (NTFS ACLs govern access here — POSIX modes are not enforced)
```

Status `ok`. Do not invent an ACL check; it would be new, untested, platform-specific
security code for a file already inside a per-user profile directory.

### 3.4 One required change: atomic status writes on Windows

`statusfile.write_status()` uses `os.replace()`. On Windows `os.replace()` raises
`PermissionError` when the destination is **open by another process** — and
`cli.status()`/`_wait_for_fresh_status()` open it exactly while the sidecar is writing.
The blanket `except Exception` already degrades this to a logged, dropped write, so it is
not a crash — but a dropped write during the restart wait can turn a healthy restart into
a false *"has not published fresh status"* warning. That is the exact class of false alarm
v0.5.3 was released to eliminate.

Add a bounded retry inside `write_status`, `win32` only: **3 attempts, 50 ms apart**, then
give up and log as today. Small, contained, and it protects the contract we just built.

---

## 4. Decision 4 — Platform guarding

### 4.1 Suppression: the guard already exists. Verify it; don't rebuild it.

`hidhelp.explain_environment()` (hidhelp.py:253–260):

```python
if not info.is_wsl:
    if sys.platform not in ("darwin", "win32") and not usbnode.udev_is_live():
        guidances.append(... U-DEAD ...)
    return guidances
```

On native Windows: `wsl.detect()` reads `/proc/sys/kernel/osrelease`, which does not exist
→ `OSError` → `is_wsl=False` (wsl.py:57). Then the `win32` exclusion skips the U-DEAD
branch. **No WSL / usbipd / udev text fires on native Windows today.** Likewise
`cli.check_hid_openable()` (cli.py:509–512) and `init_wizard` (init_wizard.py:387) both
already exclude `win32` from the udev hints, and `service.udev_rule_exists()` returns
`False` because `/etc/udev/rules.d` does not exist.

So the answer to "how is WSL guidance suppressed without duplicating logic" is: **it
already is.** The work is to *pin* that with tests (`sys.platform` monkeypatched to
`win32`, `/proc/sys/kernel/osrelease` absent → `explain_environment() == []`), so a future
change cannot quietly regress it.

### 4.2 Replacement: a Windows branch inside `explain_environment()`

One insertion point, one new private function, all strings staying in `hidhelp.py` (P6 —
"Don't duplicate a copy of this text in a new surface"). Immediately after the
`if not info.is_wsl:` line:

```python
if sys.platform == "win32":
    return _windows_guidance(allow_usbipd_query=allow_usbipd_query)
```

`_windows_guidance()` returns `[]` on a healthy box — same contract as the rest of the
module, so `doctor` output stays quiet when nothing is wrong.

| State | Detect | Message content |
|---|---|---|
| `WIN-ELGATO` | A process named `StreamDeck` is running (`tasklist` / `Get-Process`) | The official Elgato Stream Deck app holds exclusive HID access. Close it (system tray → Quit), then `muxplex-deck service restart`. **This is the #1 native-Windows failure and it is cheaply detectable — say it specifically instead of "no device found".** |
| `WIN-USBIP` | `usbipd.exe list` shows Elgato VID `0fd9` in state `Attached` | The deck is currently handed to WSL; Windows cannot see it. `usbipd detach --busid <id>` — **no admin needed** (only `bind` requires it). Then: **physically unplug and replug the deck.** Verified today: after `detach`, the deck keeps rendering whatever WSL last drew and native Windows cannot claim it until it is power-cycled. That is a real step, not a quirk — it belongs in the tool, not in a README nobody reads. |
| `WIN-NOHIDAPI` | `hidapi_win.ensure_hidapi()` returned `None` | The bundled HIDAPI is missing (arm64 Windows, or a source checkout without the vendored blob). Point at `https://github.com/libusb/hidapi/releases` and the `./hidapi.dll`-in-CWD fallback the loader honors. |
| `WIN-DLL-SHADOW` | `ctypes.util.find_library("hidapi")` ≠ our vendored path | Name **both** paths and say which one won. This is the failure mode §2.1 creates; it must be visible. |

Reuse `wsl.find_usbipd()` / `wsl.list_devices()` / `wsl._parse_list_output()` as-is for
`WIN-USBIP` — the `usbipd.exe list` output is the same text whether invoked from WSL or
native PowerShell. `wsl.find_usbipd()` will resolve `usbipd.exe` on the Windows PATH
without change. **`wsl.attach()` is never called from this path** — it stays the one
mutating function, and it stays WSL-only. Honor `allow_usbipd_query=False` for both
probing states.

Naming note: `wsl.py`'s usbipd helpers are now used from a non-WSL context. Do **not**
rename or move the module (churn across `hidhelp`, `cli`, `service`, `main`,
`init_wizard`, and 3 test files for zero behavior change). Add one line to its docstring
saying the usbipd helpers are also used from native Windows.

### 4.3 Small correctness fixes on the Windows path

| Site | Problem | Fix |
|---|---|---|
| `cli.update()` cli.py:1071–1085 | Hardcoded `launchctl` / `else: systemctl` branch. On Windows the `else` runs `subprocess.run(["systemctl", ...])`, which raises `FileNotFoundError` — `check=False` suppresses a non-zero *exit*, not a failed *exec*. **This is latent today**: the block is guarded by `was_active`, and `service_is_active()` currently returns `False` on Windows (its `systemctl` call hits the same `FileNotFoundError`, which it *does* catch). §1.4 makes `service_is_active()` return `True` — **at which point this becomes a live traceback on every `update` with a running service.** It must be fixed in the same change that enables it. | Replace the whole inline block with `service.service_stop()`, which already dispatches per platform. Strictly less code, one dispatch site, and it removes a duplicated stop implementation. |
| `cli._find_uv()` cli.py:967 | POSIX-only candidate paths. | `shutil.which("uv")` already handles `PATHEXT`; add `Path.home()/".local"/"bin"/"uv.exe"` to the fallback list. |
| `cli.check_service_status()` cli.py:662 | Hardcodes systemd/launchd. | Add a `win32` branch: manager `"Task Scheduler"`, tool `schtasks.exe`. |
| `cli.check_ca_file()` cli.py:346 | Shells to `openssl`, absent by default on Windows → permanent "cannot verify" warn, exactly where the CA-vs-leaf mistake is most likely. | **Accept the warn** — do not add a `cryptography` dependency for one check. Soften the Windows message to be actionable: `winget install ShiningLight.OpenSSL.Light`, or note that Git for Windows ships `openssl.exe`, or verify the fingerprint on the server. |
| `cli.wsl_attach()` cli.py:1216 | On native Windows prints a generic "Not running under WSL". | Add a `win32` branch pointing at the real Windows causes (Elgato app; detach + replug) instead of a dead end. |

### 4.4 What needs no change

`focus.py:53` already gates on `darwin` and logs one INFO elsewhere. `main.py`'s hotplug
loop polls `manager.find_device()` — no `WM_DEVICECHANGE`, no window handle, works
headless in any session. `usbnode.py` returns `None`/`False` on Windows (paths absent) and
is only consulted from WSL/Linux branches. `config._expand()` handles `~\`. `pwd` import
is already guarded (config.py:22).

---

## 5. Per-command behavior on Windows

| Command | Behavior |
|---|---|
| `muxplex-deck` / `run` | Unchanged. `ensure_hidapi()` runs inside `RealDeviceManager.__init__`. New optional `--log-file`. |
| `init` | Unchanged flow. `wsl attach` offer is skipped (`hidhelp.is_wsl2()` is `False`). udev block skipped (already `win32`-excluded). `service install` offer now registers a scheduled task. Windows guidance from §4.2 appears in step 7. |
| `doctor` | Same checks + Windows lines: resolved hidapi path; Elgato-app state; usbipd state; service check reports "Task Scheduler". `check_federation_key` reports the ACL note (§3.3). `check_ca_file` softened (§4.3). |
| `status` | Unchanged logic. `service_is_active()` / `service_main_pid()` resolve via `_win_task_query()`, so the pid-vs-status comparison (cli.py:906) works identically. |
| `config *` | Unchanged. Paths render as `C:\Users\…`. |
| `update` | Unchanged, minus the two fixes in §4.3. `uv tool install --force muxplex-deck` is the same command. |
| `version` | Unchanged. |
| `wsl attach` | Still exits 1 with a Windows-specific message (§4.3). Kept, not deprecated — see §6. |
| `service install/uninstall/start/stop/restart/status/logs` | §1.6. |
| `deck-probe` | `ensure_hidapi()` added before `DeviceManager()`. Otherwise unchanged. |

---

## 6. Scope discipline — what I am deliberately NOT building

**Not building:**
- Any Windows Service / SCM integration. No `pywin32`. No WinSW, no NSSM.
- Boot-start. (Print the escalation; never run it.)
- Tray icon, GUI, or toast notifications. AGENTS.md: *"Defer UI polish until the pipe is proven."*
- MSI / installer / winget package. `uv tool install` remains the one install path.
- Windows **arm64** DLL bundling (no official prebuilt) and 32-bit Python.
- Any automatic detach-from-WSL. The tool never mutates the *other* environment's device
  claim. `wsl.attach()` remains the single mutating function in the whole surface.
- `%APPDATA%` support, config migration, or any WSL↔native config bridge.
- ACL inspection/repair for the federation key.
- Any change to the systemd/launchd code paths beyond (a) the two platform-conditional
  restart-poll constants and (b) replacing `update()`'s inline stop with `service_stop()`.
- An X.509 parser to replace `openssl` on Windows.

**Is `wsl attach` still meaningful?** Yes, unchanged. It runs *inside* WSL; native Windows
never reaches it. Native support doesn't deprecate WSL support — it ends WSL's status as
the *only* option on a Windows box. Docs should present **native as the default
recommendation** and WSL as the choice for someone whose muxplex tooling already lives
there. The USB/IP bridge, the per-attach `chown`, and the changing device number remain
real costs that native Windows simply does not have.

**Migration WSL → native: documentation, no code.** Four steps:
1. In WSL: `muxplex-deck service uninstall`
2. In Windows PowerShell: `usbipd detach --busid <id>` *(no admin)*
3. **Physically unplug and replug the deck** — verified today; without it the deck holds
   WSL's last frame and native cannot claim it.
4. In native PowerShell: `uv tool install muxplex-deck` → `muxplex-deck init` →
   `muxplex-deck service install`

Nothing is copied. `init` re-fetches the CA and re-validates the key against the server.

**Do the three topologies change on Windows? No.** `init_wizard` branches on what the
*server* offers — CA fetch at init_wizard.py:147, `federation_enabled` at :125 — and never
on the client OS. The only client-OS-dependent field in the entire config surface is
`focus_app`, already documented and gated macOS-only. One caveat worth stating: the
**all-local** topology on Windows would mean running *muxplex itself* on Windows, which is
out of scope for this repo and unverified. This spec covers the Windows **client** against
a muxplex hosted elsewhere (remote server, or WSL on the same box).

---

## 7. Module layout

```
src/muxplex_deck/
  hidapi_win.py                    NEW  — ensure_hidapi(); win32-only, ~40 lines
  service.py                       MOD  — _win_* implementations + dispatch arms
  hidhelp.py                       MOD  — _windows_guidance() + 4 message bodies
  cli.py                           MOD  — --log-file flag; 5 fixes from §4.3
  main.py                          MOD  — _configure_logging(log_file)
  statusfile.py                    MOD  — win32 retry in write_status
  config.py                        —    unchanged
  wsl.py                           MOD  — docstring only
  device_real.py                   MOD  — ensure_hidapi() call; fix the Windows message
  _vendor/hidapi/x64/hidapi.dll    NEW  — vendored binary
  _vendor/hidapi/LICENSE-bsd.txt   NEW
  _vendor/hidapi/VERSION           NEW  — upstream tag + SHA-256 of hidapi-win.zip
src/deck_probe/main.py             MOD  — ensure_hidapi() call
```

`service.py` is already 943 lines. The Windows arm adds roughly 250. If it crosses ~1200,
split the three private implementation blocks into `service_systemd.py` /
`service_launchd.py` / `service_win.py` with `service.py` keeping only the dispatchers —
but **do not** do that as part of this change. One thing at a time.

---

## 8. Testing

Everything below is fakeable — no hardware, no network, no real Task Scheduler. Keep the
existing **404 green**; expect roughly **+40–60**.

- **Pure functions, golden-tested:** task-XML generation (assert every setting from §1.2 is
  present, especially `PT0S`, both battery flags, `IgnoreNew`); `_parse_win_task_query()`
  (`MISSING`, `OK 4 12345`, `OK 3 0`, garbage, empty → never raises).
- **Dispatch:** monkeypatch `sys.platform` to `win32`; assert each `service_*` verb issues
  the expected argv via the existing `recording_run` fixture pattern
  (`test_cli_service.py`).
- **Suppression regression guard (§4.1):** `sys.platform="win32"` + absent
  `/proc/sys/kernel/osrelease` → `explain_environment()` produces no udev/WSL text.
- **`_windows_guidance()`:** each of the four states, with injected fakes.
- **`hidapi_win.ensure_hidapi()`:** fake package dir with/without the DLL; assert PATH is
  prepended, `add_dll_directory` is called with our dir, the cookie is retained, and it is
  idempotent.
- **`write_status` retry:** `os.replace` raising `PermissionError` twice then succeeding.
- **`check_federation_key` on win32:** returns `ok`, no `chmod` string in the message.

**New safety rail — required.** `conftest.py` Rail 2 redirects the systemd unit and launchd
plist paths so a careless test cannot touch a real service. Windows' artifact is not a file
but a **registered scheduled task**. Rail 4 (subprocess neutering) already blocks
`schtasks.exe` and `powershell.exe`, which is the primary protection — but add a Rail 2
extension that monkeypatches `service._WIN_TASK_NAME` to a per-test unique value, so a test
that opts in via `@pytest.mark.allow_real_subprocess` still cannot delete the developer's
real task. Per AGENTS.md, adding a rail **requires** a corresponding assertion in
`tests/test_safety_rails.py`, which fails loudly if a rail is weakened.

**CI:** the suite is Linux-only today. With `sys.platform` monkeypatched, all of the above
runs on `ubuntu-latest` unchanged — **no Windows runner is required for the unit suite**,
and adding one is out of scope. What a Windows runner *cannot* be substituted for is §9.

---

## 9. Real-hardware sign-off checklist

AGENTS.md: *"Real-hardware sign-off is mandatory… Prove device I/O on the physical deck
before calling it done."* This port must not ship on a green unit suite.

On the Windows box, in order:

1. `uv tool install muxplex-deck` → `muxplex-deck doctor` — hidapi resolves to the
   **vendored** path; deck detected; no udev/WSL text anywhere in the output.
2. `muxplex-deck init` — CA auto-fetched, key validated, config written to
   `C:\Users\<u>\.config\muxplex-deck\`.
3. `muxplex-deck run` in a console — keys render, presses switch sessions, hotplug
   (unplug/replug) recovers.
4. `muxplex-deck service install` — **no UAC prompt**, no console window appears, task
   visible in `taskschd.msc`.
5. `muxplex-deck status` — shows a pid; that pid matches Task Manager.
6. `muxplex-deck service restart` — prints the **success** line, not the "has not published
   fresh status" warning. **This is the v0.5.3 contract; it is the single most important
   check here.**
7. Log off and back on — the sidecar comes back by itself.
8. `taskkill /PID <pid> /F` — it comes back within 60 s.
9. Open the Elgato app → `doctor` names it specifically (`WIN-ELGATO`), not "no device".
10. `usbipd bind` + `attach --wsl` → `doctor` names `WIN-USBIP` and prints the detach +
    replug steps.
11. `muxplex-deck service logs` — streams, Ctrl-C exits cleanly.
12. `muxplex-deck service uninstall` — task gone from `taskschd.msc`.
13. **Regression:** the same suite of verbs on macOS and on WSL, unchanged.

---

## 10. What I could NOT verify from this Linux host

Every item here is an assumption the implementer must confirm on real Windows **before**
shipping. Listed in rough order of "how badly the design breaks if it's wrong."

1. **`pythonw.exe` exists inside a `uv tool install` venv on Windows.** If not, the
   no-console-window property is lost and the fallback (visible console) applies. *Check:*
   `dir "$env:USERPROFILE\.local\share\uv\tools\muxplex-deck\Scripts\pythonw.exe"`.
2. **Task Scheduler `EnginePID` equals the sidecar's PID** for a direct `pythonw.exe`
   action. If it reports something else, **§1.4's PID contract breaks** and `restart` must
   fall back to a capture-old-pid-then-wait-for-a-different-one scheme.
3. **`schtasks /Create /XML` encoding.** Multiple independent reports say the XML file must
   be **UTF-16 LE with a BOM** (`FF FE`) or `schtasks` rejects it as malformed; others
   report ASCII-only UTF-8 working. Spec says write UTF-16 LE + BOM — **the one place this
   repo's blanket `encoding="utf-8"` convention is deliberately violated, and it needs an
   inline comment saying why.**
4. **The exact XML settings block is accepted** — specifically `<ExecutionTimeLimit>PT0S`,
   a `<Repetition>` with no `<Duration>` meaning "indefinitely", and `<LogonTrigger>` with
   an explicit `<UserId>`. Also unverified: whether `schtasks /Create /XML` accepts
   `<LogonType>InteractiveToken</LogonType>` **without** an explicit `/RU <user>` on the
   command line. If it demands `/RU`, pass the current user; it must still never prompt
   for a password with `InteractiveToken`.
5. **`schtasks /Create` in the user's own context genuinely needs no elevation on that
   box.** Group Policy can restrict task creation. If it is blocked, the whole approach
   needs re-evaluation.
6. **The vendored `hidapi.dll` loads with only what a CPython install provides**
   (`vcruntime140.dll` / UCRT). Expected fine — CPython ships `vcruntime140.dll` in its own
   directory, which is `LOAD_LIBRARY_SEARCH_APPLICATION_DIR` — but unproven.
7. **`os.add_dll_directory` + PATH-prepend actually make streamdeck's loader pick our
   DLL.** The mechanism is derived from reading CPython and streamdeck source, not from
   running it. *Check:* `python -c "import ctypes.util, muxplex_deck.hidapi_win as h;
   h.ensure_hidapi(); print(ctypes.util.find_library('hidapi'))"`.
8. **`uv_build` includes a `.dll` as package data**, and PyPI accepts it in a pure-Python
   wheel. If not, §2.5 flips to a platform-tagged wheel and the publish workflow grows a
   Windows job.
9. **`os.replace` `PermissionError` frequency** in practice — the §3.4 retry is
   precautionary, sized from the documented Windows semantics, not from a measured rate.
10. **`Get-Content -Wait` for `service logs`** — streaming behavior and Ctrl-C handling.
11. **`usbipd.exe list` output is identical** when invoked from native PowerShell rather
    than from WSL. `wsl._parse_list_output` was written against the WSL invocation.
12. **`schtasks /End` hard-kills** rather than stopping gracefully (§1.7 predicts hard).
13. **Whether the Elgato app holds the device merely by being installed** (background
    service) or only when its window is open. Determines whether "close the app" is
    sufficient guidance or whether a service must also be stopped.
14. **Session-0 HID access** (§1.1). Moot for this design; recorded so it is not
    re-investigated.

---

## 11. Sequencing

Four independently shippable increments. Each leaves the tool working.

1. **DLL bundling + `hidapi_win.py` + `deck_probe`/`device_real` call sites.**
   Ships value immediately: `uv tool install muxplex-deck` → `muxplex-deck run` works on
   native Windows with no service at all. Verifies §10.6–10.8, the riskiest packaging
   unknowns, before any service code exists.
2. **Platform guarding (§4).** Makes `doctor` / `init` / `status` honest on Windows, still
   with no service. Verifies §10.11 and §10.13.
3. **The scheduled-task service (§1).** The bulk of the work; verifies §10.1–10.5, §10.12.
4. **README + `AGENTS.md`.** Windows quickstart, the WSL→native migration, the two honest
   trade-offs (logon-not-boot; 60 s restart), the session-0 finding, and the vendored-DLL
   bump procedure.

Do not start 3 before 1 is confirmed on hardware. If the DLL story doesn't hold, the
service story is irrelevant.
