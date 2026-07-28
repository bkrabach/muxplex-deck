# Specification: WSL cold-start guidance for muxplex-deck

Target repo: `muxplex-deck` @ `b1a241f` (v0.4.1, main, clean, published on PyPI)
Author: zen-architect
Status: **design complete — no implementation written**

---

## 0. Verification ledger (read this first)

I am on Linux (aarch64, kernel `6.17.0-1014-nvidia`, `seat0` present, udev running).
**I cannot run WSL and neither can the requester from this host.** Everything below is
tagged so the implementer knows what is load-bearing evidence and what must be proven
on the WSL box before shipping.

### VERIFIED — from this repo's source

| # | Fact | Evidence |
|---|---|---|
| V1 | No runtime WSL detection exists anywhere | grep `wsl\|microsoft-standard\|/proc/version` → only help-text literals at `cli.py:426,429,433`, `service.py:140` |
| V2 | The udev remediation is printed unconditionally on Linux install | `service.py:336` calls `_warn_if_no_udev_rule()`; `service.py:234-237` gates only on rule *file presence*, never on whether udev is running |
| V3 | The shipped rule relies on `TAG+="uaccess"` | `service.py:126-129` |
| V4 | The remediation tells the user to run `udevadm control --reload-rules` | `service.py:138` |
| V5 | The sidecar dumps a full traceback per poll cycle on open failure | `main.py:1029-1032` — `logger.exception(...)` then `shutting_down.wait(DEVICE_POLL_SECONDS)` inside `while not shutting_down.is_set()` |
| V6 | The open-failure branch never updates the status file | `main.py:1028-1032` — no `reporter.update(...)`, unlike the `deck is None` branch at `main.py:1009-1011`. **This is why the user saw "Status file is stale (31s ago)" and a bogus "Server: unreachable".** |
| V7 | The only non-dummy HID transport is libusb | `StreamDeck/DeviceManager.py` → `transports = {"dummy": Dummy, "libusb": LibUSBHIDAPI}`. There is **no hidraw code path**, so the `SUBSYSTEM=="hidraw"` line in `service.py:128` is dead weight for this library |
| V8 | The exact failure is `LibUSBHIDAPI.py:223` | `open_device()` → `hid_open_path()` returns NULL → `raise TransportError("Could not open HID device.")`. Reached from `RealDeckDevice.open()` (`device_real.py:49`) |
| V9 | Enumeration succeeds without open permission | Observed: the sidecar got past `find_device()` and failed at `deck.open()` |
| V10 | `_NO_DEVICE_GUIDANCE` shows WSL/udev text on **all** platforms, says "admin PowerShell" for all three usbipd commands, and writes `usbipd` without `.exe` | `cli.py:422-434` |
| V11 | `check_hid_openable`'s Linux hint recommends `service install` → i.e. the udev block | `cli.py:506-509` |
| V12 | `status` falls back to a direct probe when the service is down | `cli.py:734-736, 768-783` |
| V13 | The test suite auto-neuters `subprocess.run` for `systemctl/launchctl/loginctl/openssl/git/uv/pip` only | `AGENTS.md` rails table; `usbipd` is **not** on that list |
| V14 | There is no `docs/` directory; README's "Windows + WSL" block is at `README.md:147-171` | `ls`, `grep` |

### VERIFIED — from this host

| # | Fact | Evidence |
|---|---|---|
| V15 | `udevadm control` talks to `/run/udev/control`; its error string is `Failed to send reload request: %m` | `strings $(command -v udevadm)` → both literals present |
| V16 | `/run/udev/control` is a `srw------- root root` socket inside a `drwxr-xr-x` dir → **any user can `stat()` it** | `ls -l /run/udev/control` as non-root succeeded |
| V17 | `/sys/bus/usb/devices/*/{idVendor,idProduct,busnum,devnum}` yields everything needed to build `/dev/bus/usb/%03d/%03d` | enumerated live on this host |
| V18 | `plugdev` group exists on Ubuntu | `getent group plugdev` |

### VERIFIED — external, authoritative

| # | Fact | Source |
|---|---|---|
| V19 | On WSL, `udevadm control --reload-rules` fails with `Failed to send reload request: No such file or directory`, and udev rules are silently ignored | microsoft/WSL#8502 (open→closed, exactly this symptom) |
| V20 | usbipd-win's own wiki tells users they "may need to configure udev rules to allow non-root users to access the device" — i.e. this is expected, not a bug | dorssel/usbipd-win wiki, *WSL support* |
| V21 | The working community pattern on WSL is `MODE=` + `GROUP=`, plus restarting udev **before** attaching — never `uaccess` | usbipd-win discussion #347; tonymitchell.ca AVR/WSL writeup |
| V22 | `usbipd state` emits full state as JSON; added in usbipd-win **2.2.0**. `usbipd list` is explicitly documented as human-readable and truncating | dorssel/usbipd-win wiki, *Automation* |

Combining **V15 + V16 + V19**: `Path("/run/udev/control").exists()` is a cheap, non-root,
deterministic probe for *"will a udev rule ever fire on this machine."* This is the single
most important mechanism in this spec.

### NOT VERIFIED — implementer must confirm on the WSL box

| # | Claim | Why it matters | How to check |
|---|---|---|---|
| U1 | `TAG+="uaccess"` is inert on WSL because there is no logind seat | Justifies changing the shipped rule | `loginctl list-seats` in WSL — expect `0 seats listed` |
| U2 | The user's rule *did* fire (mode became `0660`) but produced no ACL — vs. never fired at all | devtmpfs default for USB nodes is `0600`; the observed `crw-rw---- root root` is `0660`, which is what the rule sets. Either way the fix is the same, but it tells us whether udev was running | `ls -l /run/udev/control`; `getfacl /dev/bus/usb/001/003` |
| U3 | `usbipd.exe list`'s "Connected:" section columns are `BUSID  VID:PID  DEVICE  STATE` | The parser depends on it | Run it; ground truth already shows `1-4  0fd9:006d ... Shared` |
| U4 | `usbipd.exe state` JSON field names (`Devices[].BusId`, `.InstanceId`, `.PersistedGuid`, `.ClientIPAddress`) | Only needed if the optional hardening in §6.3 is taken | `usbipd.exe state` |
| U5 | `/sys/bus/usb/devices/` is populated for usbip-attached devices in WSL | The node resolver depends on it | after attach: `ls /sys/bus/usb/devices/*/idVendor` |
| U6 | `usbipd bind` fails or misbehaves while the Elgato app holds the device | Determines where we place the "close the Elgato app" line | attempt a bind with the app running |
| U7 | Enabling `[boot] systemd=true` actually makes the rule fire for a *subsequently attached* usbip device | This is the recommended durable fix | full loop: edit wsl.conf → `wsl.exe --shutdown` → attach → `ls -l` the node |

**Rule for the implementer:** every string in §7 that asserts a *durable* fix (U7) must
be proven end-to-end before release. If U7 fails, delete the durable-fix block and ship
only the per-attach path — the CLI must never teach something it hasn't been shown to work.
That is the exact failure this whole spec exists to correct.

---

## 1. Problem statement

A cold-start WSL user gets actively misled, twice, by the tool itself:

1. `service install` prints a udev remediation whose *very first command* (`udevadm control
   --reload-rules`) fails on WSL (V4 + V19), whose rule mechanism (`uaccess`) cannot work
   without a seat (V3 + U1), and whose `hidraw` line is irrelevant to the only transport
   the library has (V7).
2. `TransportError: Could not open HID device.` names neither the device node nor the fix,
   and repeats with a full traceback every 2 seconds (V5), burying the journal — while the
   status file goes stale and reports a *false* "Server: unreachable" (V6).

Meanwhile the three genuinely WSL-specific steps (bind / attach / re-attach) exist only in
prose, in the wrong place, with the wrong binary name.

**Goal:** a user who runs `uv tool install muxplex-deck` and then any of
`init` / `doctor` / `status` / `service install` / `run` is told exactly what to do next,
in copy-pasteable form with real values substituted, without ever opening the README.

---

## 2. Design principles (these settle every later decision)

**P1 — Branch on capability, not on platform.**
The permission remediation branches on *"is udev running"* (`/run/udev/control`), not on
*"is this WSL."* WSL detection is used only to add USB/IP knowledge that genuinely does not
exist elsewhere. This is why the fix also repairs plain-Linux containers, and why it won't
rot when WSL gains udev.

**P2 — Diagnostics read; only explicitly-named commands write.**
`doctor`, `status`, `run` never mutate host state. They *may* run read-only queries,
including shelling out to `usbipd.exe list`, because listing is a read.

**P3 — The tool never elevates. No `sudo`, ever, not even offered.**
Invoking `sudo` from a CLI hangs in non-tty contexts (systemd units, `update`'s nested
`service_install()`), depends on sudoers config, and surprises the user with a password
prompt inside a command that didn't advertise one. Every privileged action is *printed*,
never executed. This is a hard invariant, and it preserves the repo's existing
"never writes to `/etc`" promise (`service.py:219`).

**P4 — Never print a command we know will fail here.**
If `/run/udev/control` is absent, the udev block is not printed at all — not printed with
a caveat. A caveat is still an instruction, and the observed cost of a wrong instruction
was ~40 minutes.

**P5 — Resolve real values; `<BUSID>` and `<NODE>` are failures.**
Every placeholder we can fill, we fill. When we genuinely cannot (usbipd.exe absent), we
degrade to instructions — never to an error.

**P6 — One home for the strings.**
Four surfaces (`doctor`, `status`, `service install`, the sidecar, plus `init`) must say the
same thing. They compose from one module; no surface writes its own copy.

---

## 3. The state machine

Classification is a pure function of four observations:

- `is_wsl`, `wsl_version` — from `/proc/sys/kernel/osrelease`
- `usbipd` — `absent | present(path)`, plus `impostor_present`
- `usbipd_device` — `none | not_shared(busid) | shared(busid) | attached(busid) | unknown`
- `node` — `absent | present(path, mode, accessible: bool)` — from sysfs (V17)
- `udev_live` — `Path("/run/udev/control").exists()` (V15/V16)

| State | Condition | Meaning | CLI must produce |
|---|---|---|---|
| **W0** | not WSL | macOS / native Linux | Existing behavior, minus the WSL text now removed from `_NO_DEVICE_GUIDANCE` (§7.7) |
| **W1** | WSL1 | osrelease has `microsoft` but not `WSL2` | Hard stop: USB/IP is WSL2-only |
| **W2** | WSL2, `usbipd.exe` absent | no interop, or usbipd-win not installed | `winget` install line + interop note |
| **W3** | WSL2, usbipd present, no `0fd9` device listed | not plugged into the PC, or Windows can't see it | "plug it in / check Device Manager" |
| **W4** | listed, state `Not shared` | needs `usbipd bind` — **requires elevation we cannot do** | Elevated-PowerShell bind line with real BUSID + close-Elgato-app note |
| **W5** | listed, state `Shared` | bound, not attached — **no elevation needed** | `muxplex-deck wsl attach` (tool can do this) |
| **W6** | listed, state `Attached`, node absent from sysfs | attached to *a different* WSL distro, or vhci glitch | detach/re-attach line |
| **W7** | node present, not accessible | **the permission wall** | resolved node + `chown` line + durable options |
| **W8** | node accessible, open still fails | something else holds it | existing service-holds-it logic, then Elgato-app / other-process |
| **W9** | everything works | — | existing ✓ lines |

Orthogonal, evaluated independently of W0–W9:

| State | Condition | CLI must produce |
|---|---|---|
| **U-DEAD** | Linux and `not udev_live` | "udev is not running → rules will never fire here" + the enable-systemd path. **Suppresses the udev remediation entirely (P4).** |
| **U-LIVE** | Linux and `udev_live` and no rule | today's remediation, with the improved rule (§8) |
| **IMPOSTOR** | WSL and both `usbipd` and `usbipd.exe` resolve | the disambiguation block (§7.6) |

---

## 4. Module design (bricks & studs)

Three new modules under `src/muxplex_deck/`. Each is independently testable with no
hardware, no WSL, no network.

### 4.1 `usbnode.py` — pure Linux USB facts (~90 lines)

No WSL knowledge. No subprocess. No prose.

```
find_usb_node(vendor_id: str = "0fd9", *, sysfs_root: Path = Path("/sys/bus/usb/devices"),
              dev_root: Path = Path("/dev/bus/usb")) -> UsbNode | None
udev_is_live(*, control_path: Path = Path("/run/udev/control")) -> bool

@dataclass(frozen=True)
class UsbNode:
    path: Path          # /dev/bus/usb/001/003
    vendor_id: str      # "0fd9"
    product_id: str     # "006d"
    busnum: int
    devnum: int
    mode: int | None    # 0o660
    owner_uid: int | None
    readable_writable: bool   # os.access(path, R_OK | W_OK)
```

- Iterates `sysfs_root/*/idVendor`, matches case-insensitively, reads `idProduct`,
  `busnum`, `devnum`; builds `dev_root / f"{busnum:03d}" / f"{devnum:03d}"`.
- Every default is an injectable parameter → tests build a fake tree in `tmp_path`. **No new
  autouse safety rail needed**, because nothing here can touch a real device.
- Returns `None` on any `OSError`. Never raises.
- Deliberately does **not** use `StreamDeck`'s `device_info['path']`: that is hidapi's
  `bus:addr:iface` hex string whose format varies across hidapi releases. sysfs is
  kernel-provided and version-independent (V17).

### 4.2 `wsl.py` — WSL + usbipd (~150 lines)

```
detect() -> WslInfo                       # {is_wsl, version: 1|2|None, kernel: str}
find_usbipd() -> UsbipdPaths              # {windows: Path|None, linux_impostor: Path|None}
list_devices(usbipd: Path, *, timeout=5.0) -> list[UsbipdDevice] | None
attach(usbipd: Path, busid: str, *, timeout=30.0) -> tuple[bool, str]   # ONLY mutating fn
wsl_conf_systemd_state() -> "enabled" | "boot-section-exists" | "absent" | "unreadable"

@dataclass(frozen=True)
class UsbipdDevice:
    busid: str          # "1-4"
    vid_pid: str        # "0fd9:006d"
    description: str
    state: "not_shared" | "shared" | "attached" | "unknown"
```

- `detect()` reads `/proc/sys/kernel/osrelease` **only**. Do **not** use `WSL_DISTRO_NAME` /
  `WSL_INTEROP`: those are shell-injected and absent under a systemd user unit — the exact
  context the sidecar runs in.
- `find_usbipd()` returns both: `shutil.which("usbipd.exe")` and `shutil.which("usbipd")`.
  If the latter resolves to something that is not the former, it is the impostor.
- `list_devices()` invokes the resolved **`usbipd.exe` absolute path** — never the bare name
  `usbipd` (P5 / decision §6.4). Parses only the `Connected:` section; splits leading BUSID
  and VID:PID off the front and matches the trailing state case-insensitively against
  `{"not shared", "shared", "attached"}`. Anything unmatched → `state="unknown"`, which the
  caller degrades to manual instructions rather than guessing.
- Returns `None` (not `[]`) on timeout / `FileNotFoundError` / `OSError` /
  `Exec format error` (interop disabled) — `None` means "could not query", `[]` means
  "queried, nothing connected." The two produce different messages (W2 vs W3).
- `wsl_conf_systemd_state()` reads `/etc/wsl.conf` so the tool can print *append* vs *edit*
  correctly instead of a blind `tee -a` that could produce a duplicate `[boot]` section.

### 4.3 `hidhelp.py` — the product surface (~180 lines, mostly strings)

The single home for every string in §7. Imports `usbnode` and `wsl`; imported by `cli`,
`service`, `main`, `init_wizard`.

```
@dataclass(frozen=True)
class Guidance:
    status: "ok" | "warn" | "fail"   # matches cli.print_check
    message: str                     # multi-line; caller does the indenting
    state: str                       # "W4", "U-DEAD", ... for tests + telemetry

explain_environment(*, allow_usbipd_query: bool = True) -> list[Guidance]
explain_open_failure(error: str, *, allow_usbipd_query: bool = True) -> Guidance
udev_guidance() -> Guidance | None       # replaces service._UDEV_REMEDIATION
```

- `allow_usbipd_query=False` is the escape hatch for contexts that must not shell out.
- All formatting stays compatible with `cli.print_check` (mark on line 1, 4-space
  continuation) — **except heredoc bodies, which must be emitted flush-left** so the
  pasted content is not polluted with leading spaces. `service.py:136-137` already
  establishes this convention; follow it, and add a comment saying why.

### 4.4 Files touched

| File | Change |
|---|---|
| `cli.py` | `doctor()` gains an environment section; `_NO_DEVICE_GUIDANCE` loses its WSL/udev text; `check_hid_openable`'s hint delegates to `hidhelp`; new `wsl attach` subparser + dispatch |
| `service.py` | `_warn_if_no_udev_rule()` → delegates to `hidhelp.udev_guidance()`; `_UDEV_RULE_CONTENT` updated (§8); `_systemd_install` prints environment guidance too |
| `main.py` | open-failure branch: guidance + once-only traceback + status update (§9) |
| `statusfile.py` | one new optional field `device.hint` (§9.3) |
| `init_wizard.py` | step 7 uses `hidhelp`; offers `wsl attach` at the prompt |
| `tests/conftest.py` | add `usbipd`/`usbipd.exe` to the subprocess-neutering blocklist (§11) |
| `tests/test_safety_rails.py` | assert the new blocklist entries |
| `README.md` | replace lines 147-171; link `docs/WSL.md` |
| `docs/WSL.md` | new |
| `AGENTS.md` | record the udev/uaccess/WSL lesson |

---

## 5. Per-command behavior

| Command | W0 (not WSL) | W2 usbipd absent | W4 not shared | W5 shared | W7 no permission | U-DEAD |
|---|---|---|---|---|---|---|
| `doctor` | unchanged | §7.1 | §7.2 | §7.3 | §7.4 | §7.5 |
| `status` (service down) | unchanged | §7.1 | §7.2 | §7.3 | §7.4 | §7.5 |
| `status` (service up) | unchanged | shows `device.hint` from the status file — no re-query | ← | ← | ← | ← |
| `service install` | unchanged | §7.1 after the ✓ steps | §7.2 | §7.3 | §7.4 | §7.5 **instead of** the udev block |
| `run` (sidecar) | unchanged | logs §7.1 once | §7.2 once | §7.3 once | §7.4 once | §7.5 once |
| `init` | unchanged | §7.1 | §7.2 | §7.3 + prompt | §7.4 | §7.5 |
| `wsl attach` | error: not WSL | §7.1, exit 1 | §7.2, exit 1 | **attaches**, exit 0 | attaches then §7.4, exit 0 | appends §7.5 |
| `update` | unchanged (inherits via its nested `service_install()` + `doctor()`) | | | | | |
| `config`, `version` | unchanged | | | | | |

Placement inside `doctor`: the environment guidance is emitted **before**
`check_deck_detected` — it explains why the next line is about to fail. On W0 with
`udev_live`, `explain_environment()` returns `[]` and `doctor`'s output is byte-identical
to today's. **No new noise for macOS or healthy-Linux users.**

---

## 6. The auto-remediation boundary — the central call

### 6.1 What each command may do

| | read sysfs / `/run/udev` | run `usbipd.exe list` | run `usbipd.exe attach` | chmod/chown | sudo |
|---|---|---|---|---|---|
| `doctor` | yes | **yes** | no | no | **never** |
| `status` | yes | yes (only on the service-down probe path) | no | no | never |
| `run` | yes | yes, **once** per failure episode | no | no | never |
| `service install` | yes | yes | **no** | no | never |
| `init` | yes | yes | **yes — after an explicit `y/N` prompt** | no | never |
| `wsl attach` | yes | yes | **yes — this is its entire purpose** | no | never |

### 6.2 Rationale

**Why `doctor` may run `usbipd.exe list`.** Listing is a read. It is bounded (5s timeout),
total-failure-tolerant, and it is the single thing that converts `<BUSID>` into `1-4`. A
diagnostic that refuses to look is not more principled, only less useful. The invariant
diagnostics must hold is *don't change the world* — not *don't observe it*.

**Why `doctor`/`service install` may NOT attach.** `usbipd attach` mutates the **Windows
host's** device topology: it takes the device away from Windows and hands it to this VM. If
the Elgato app or another Windows tool is using the deck, that is a visible, cross-OS
side-effect. A command named "install a service" must not do that. It is doubly wrong for
`service install` because `update()` calls it (`cli.py:986`) — a re-attach as a side-effect
of `muxplex-deck update` would be genuinely astonishing.

**Why a dedicated `muxplex-deck wsl attach` instead of nothing.** The user asked the tool to
"do what it can." An explicitly-named mutating command does exactly that without hiding the
mutation. And it carries a second, larger benefit: **the user never types `usbipd` at all**,
so they cannot fall into the impostor trap (§6.4). That alone justifies the subcommand.

**Why `init` may attach.** `init` is already the interactive, mutating wizard that offers
to run `service install`. Same shape, same consent model: prompt, then act.

**Why `run` may never attach.** It is a daemon under systemd. Daemons that reach out and
re-plug USB devices are how you get a device that flaps between Windows and Linux forever.

**Why we never invoke `sudo` (P3).** A password prompt inside `muxplex-deck doctor` breaks
piping, hangs under systemd, and is the kind of "helpful" behavior that becomes a support
burden. Printing the command costs the user one paste and costs us zero failure modes.

**Degradation.** `usbipd.exe` absent → `list_devices()` returns `None` → state W2 → we print
instructions. Never an error, never a traceback, never a non-zero exit from `doctor`
(`doctor` keeps returning 0; it is informational — `cli.py:649`).

### 6.3 Parsing: `list` now, `state` later

Use **`usbipd.exe list`** as the primary parser. Justification: its output is directly
evidenced by the ground-truth session (`1-4  0fd9:006d ... Shared`), and the three fields we
read — BUSID, VID:PID, trailing STATE — are precisely the ones that *cannot* be truncated
(V22 warns only about the DEVICE description column, which we ignore).

`usbipd.exe state` (JSON, V22) is a **later hardening**, not v1. It requires usbipd-win
≥2.2.0, its field names are unverified (U4), and the `IsBound`/`IsAttached` booleans the
wiki advertises are computed by the PowerShell module, not present raw. Do not build against
an unverified schema when a verified one is in hand.

### 6.4 The `usbipd` vs `usbipd.exe` trap

Three layers, in order of value:

1. **Avoidance.** `muxplex-deck wsl attach` means the common path never types the name.
2. **The tool always uses the resolved absolute path of `usbipd.exe`.** It never invokes the
   bare name. Not a message — a code invariant.
3. **Printed instructions always spell `usbipd.exe`**, everywhere, including the elevated
   PowerShell block. (Inside PowerShell on Windows the `.exe` is redundant but harmless;
   uniformity is worth more than brevity here.)
4. **Active detection.** Only when both binaries resolve, print §7.6 naming both real paths.

Do **not** print an impostor warning unconditionally — it would be noise for the majority
who never installed `linux-tools-common`. Detection is cheap and exact; use it.

---

## 7. Exact message text

Style contract: line 1 carries the mark (`  ! ` / `  ✓ `) via `cli.print_check`;
continuation lines are indented 4 spaces; **heredoc bodies are flush-left** (see §4.3).

### 7.1 W2 — WSL2, `usbipd.exe` not found

```
WSL detected -- USB devices are invisible to Linux until Windows hands them over,
and I can't find usbipd.exe to check or do that for you.
On Windows, in any PowerShell, install the bridge once:
    winget install --interactive --exact dorssel.usbipd-win
Then come back here and run:
    muxplex-deck wsl attach
If usbipd-win IS already installed, WSL's Windows-interop is probably switched
off for this distro -- see docs/WSL.md ("usbipd.exe not found").
```

### 7.2 W4 — device present on Windows, not shared

```
Stream Deck is plugged into Windows (BUSID 1-4, 0fd9:006d) but not shared with WSL.
Sharing needs administrator rights, which I can't get for you. On Windows, open
PowerShell **as Administrator** and run:
    usbipd.exe bind --busid 1-4
Close the Elgato Stream Deck app first -- it holds the device open.
Then come back here (no admin needed) and run:
    muxplex-deck wsl attach
```

### 7.3 W5 — shared, not attached

```
Stream Deck is shared but not attached to WSL (BUSID 1-4).
Run:
    muxplex-deck wsl attach
That's it -- no administrator rights needed for this step.
```

### 7.4 W7 — attached, node not accessible (the permission wall)

`{node}`, `{mode}`, `{user}` substituted from `usbnode.find_usb_node()`.

```
Stream Deck is attached, but this user can't open it.
    Device node: /dev/bus/usb/001/003   (crw-rw---- root root)
Grant yourself access:
    sudo chown "$(id -un)" /dev/bus/usb/001/003
Then:
    muxplex-deck service restart
Heads up: the device number changes on EVERY attach, so this is a per-attach step.
After any unplug, or after a Windows reboot: muxplex-deck wsl attach, then chown again.
```

`chown` over `chmod 666`: identical durability, strictly less exposure, and mechanically
certain either way — the node is `root:root` with owner-rw bits set (`0660` observed,
`0600` if udev never touched it), so taking ownership grants read+write in both cases.
`chmod 666` (what the user proved) goes in `docs/WSL.md` as the alternative.

### 7.5 U-DEAD — udev is not running

Three variants keyed on `wsl_conf_systemd_state()`. Note the single-line `printf` form: a
heredoc here would either bake `print_check`'s 4-space indent into `/etc/wsl.conf` or force
an ugly flush-left break. `printf` avoids the problem entirely.

**(a) `absent` — no `/etc/wsl.conf`, or no `[boot]` section:**

```
udev is not running here (no /run/udev/control), so udev rules will never fire --
this is normal for a WSL distro without systemd, and for containers.
That's why "udevadm control --reload-rules" fails with "No such file or directory".
Durable fix -- turn on systemd (which starts udev):
    printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
Then, in Windows PowerShell:
    wsl.exe --shutdown
Reopen this distro and run:  muxplex-deck wsl attach
You can skip all of that -- the per-attach `sudo chown` above works fine on its own.
```

**(b) `boot-section-exists`:** identical, except the first command becomes:

```
Add `systemd=true` under the existing [boot] section in /etc/wsl.conf:
    sudo nano /etc/wsl.conf
```

**(c) `enabled` — systemd is configured but udev still isn't up:**

```
udev is not running (no /run/udev/control) even though /etc/wsl.conf already has
systemd=true. The distro probably hasn't been restarted since that was set.
In Windows PowerShell:  wsl.exe --shutdown
Then reopen this distro and run:  muxplex-deck wsl attach
Until then, the per-attach `sudo chown` above is the way through.
```

### 7.6 IMPOSTOR — two programs named `usbipd`

```
Two different programs named `usbipd` are on your PATH:
    /usr/bin/usbipd                              <- Linux USB/IP daemon (linux-tools-common)
    /mnt/c/Program Files/usbipd-win/usbipd.exe   <- the Windows bridge -- THIS is the one
Always type `usbipd.exe`. The bare name is the Linux one; it will tell you to install
`linux-tools-<kernel>`, a package that does not exist for the WSL kernel.
(Or just use `muxplex-deck wsl attach` and never type either.)
```

### 7.7 Revised `_NO_DEVICE_GUIDANCE` (`cli.py:422-434`)

Strip the WSL and udev paragraphs — they are now produced by `hidhelp` in the states where
they are *true*, rather than unconditionally on every platform (V10). What remains:

```
No Stream Deck found. Things to check:
    - Close the official Elgato Stream Deck app -- it holds exclusive HID access,
      so muxplex-deck cannot open the device while it is running.
    - Check the USB cable and try a different port.
```

### 7.8 `muxplex-deck wsl attach` — success transcript

```
muxplex-deck wsl attach

  ✓ WSL2 detected (6.6.87.2-microsoft-standard-WSL2)
  ✓ usbipd.exe: /mnt/c/Program Files/usbipd-win/usbipd.exe
  ✓ Found on Windows: BUSID 1-4  0fd9:006d  Elgato Stream Deck
  ✓ Shared -- attaching...
  ✓ Attached
  ✓ Visible to Linux: /dev/bus/usb/001/003
  ! You can't open that node yet (crw-rw---- root root). Run:
      sudo chown "$(id -un)" /dev/bus/usb/001/003

  Next:
    muxplex-deck service restart
    muxplex-deck status

  The device number changes on every attach -- re-run `muxplex-deck wsl attach`
  after any unplug or Windows reboot.
```

Exit 0 on a successful attach (even if §7.4 follows — the attach *did* succeed).
Exit 1 for W1/W2/W3/W4/W6, so scripts can branch.

### 7.9 Sidecar log, first open failure (replaces V5's traceback storm)

```
ERROR muxplex-deck: cannot open the Stream Deck: Could not open HID device.
  Device node: /dev/bus/usb/001/003 (crw-rw---- root root) -- this user has no access.
  Fix:  sudo chown "$(id -un)" /dev/bus/usb/001/003
  Then: muxplex-deck service restart
  (full traceback at DEBUG level)
```

Then, at the existing `ABSENT_HEARTBEAT_SECONDS` cadence:

```
INFO  muxplex-deck: still cannot open the Stream Deck (attempt 47, same error)
```

---

## 8. The udev rule change

Current (`service.py:126-129`):

```
SUBSYSTEM=="usb",    ATTRS{idVendor}=="0fd9", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", MODE="0660", TAG+="uaccess"
```

Proposed:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

Three changes, each independently justified:

1. **Drop the `hidraw` line.** The library has no hidraw transport (V7). It was never doing
   anything for this tool.
2. **Add `GROUP="plugdev"`.** `uaccess` grants an ACL only to a user on an *active local
   seat*; WSL has no seat (U1). `GROUP` works with or without logind. Keeping `uaccess`
   preserves correct behavior on desktop Linux (where it is the modern right answer), so
   the rule now satisfies both worlds with one line. If `plugdev` is absent (Fedora/Arch),
   udev logs a warning and the group falls back to `root` — i.e. exactly today's behavior,
   never worse (V18 confirms it exists on Ubuntu, the WSL default).
3. **Print a group-membership check alongside it**, since a rule the user isn't a member of
   is the next silent failure:

```
      sudo usermod -aG plugdev "$(id -un)"     # then log out and back in
```

`udev_rule_exists()` (`service.py:214`) is unchanged — it matches on the vendor id, which
still appears.

**Gate (P4):** `hidhelp.udev_guidance()` returns `None` when `udev_is_live()` is False. The
udev block is then never printed on a machine where it cannot work, and §7.5 takes its place.

---

## 9. Sidecar changes (`main.py`, `statusfile.py`)

### 9.1 Log once, not per cycle

Add a tiny suppressor beside the existing `logged_waiting` / `last_heartbeat` pattern
already in `main.run()` — same shape, no new abstraction:

- On the first failure of an episode: `logger.error(guidance.message)` +
  `logger.debug("open failed", exc_info=True)`.
- On subsequent failures with the same signature (`type(exc).__name__ + str(exc)`):
  silent, except a one-line count at `ABSENT_HEARTBEAT_SECONDS`.
- Reset the episode on success, or when the signature changes.

Apply the same helper to the enumerate-failure branch (`main.py:1000-1005`) — same tight
loop, same problem. **Leave the active-session `logger.exception` at `main.py:1046` alone:**
it is not in a tight loop and its traceback is genuinely diagnostic.

### 9.2 Do not re-query `usbipd.exe` per cycle

`explain_open_failure()` is called once per *episode*, not per cycle. Shelling to a Windows
binary every 2 seconds forever is its own bug.

### 9.3 Update the status file on open failure (fixes V6)

The open-failure branch must call:

```
reporter.update(device_connected=False, device_caps=None,
                server_connected=False, hint=guidance.message)
```

Add one optional field, `device.hint: str | None`, to `statusfile.build_status`. `status`
renders it under the `Device:` line via `print_check("warn", hint)`.

**No `SCHEMA_VERSION` bump** — the field is additive and optional, and `_format_device_line`
(`cli.py:701`) already reads with `.get()`. An older reader ignores it cleanly.

This single change is what turns the observed

```
! Status file is stale (last updated 31s ago) -- the sidecar may be stuck
! Device: not connected
! Server: https://spark-1:8088 (unreachable)      <- false; server was fine
```

into a fresh status file that says exactly what is wrong and how to fix it. It also removes
the misleading "unreachable" line, which is only there because the loop never got far enough
to try the server.

---

## 10. Docs: what lives where

**The CLI alone must get a cold-start user to working.** `docs/WSL.md` is a supplement,
never a prerequisite. Test of correctness: if the doc were deleted, every §7 message still
contains a complete, executable next step. It does.

### `docs/WSL.md` (new) — for depth the CLI shouldn't carry

- Full annotated walkthrough with expected output at each step.
- **Durable-options comparison** with honest trade-offs:
  | Option | Survives re-attach | Cost | Verified |
  |---|---|---|---|
  | systemd + udev rule + `plugdev` | yes | `wsl --shutdown`, group logout/login | **U7 — must be proven** |
  | per-attach `sudo chown` | no | one command per attach | yes (session ground truth) |
  | run the sidecar under `sudo` | yes | no user service; sudo-aware config paths | yes (ALIENWARE box) |
- Why `usbipd` and `usbipd.exe` are different programs, and what the impostor's error looks
  like verbatim, so search engines find it.
- BUSID moves between physical ports; DEVICE number changes on every attach.
- Interop-disabled / `Exec format error` troubleshooting.
- `chmod 666` as the alternative to `chown`.
- WSL1: not supported, and why.

### `README.md` — replace lines 147-171

Delete the current block: it uses bare `usbipd`, says "admin PowerShell" for all three
commands (wrong for `attach`), and prints a `hidraw` rule that does nothing (V7). Replace
with ~6 lines:

```
#### Windows + WSL

USB devices are invisible to WSL until Windows hands them over. muxplex-deck
handles this for you:

    muxplex-deck wsl attach     # finds the deck, attaches it, tells you what's left
    muxplex-deck doctor         # diagnoses every remaining step, with real values

Neither needs the details -- but see docs/WSL.md if you want them.
```

Also fix `README.md:98-100`, which currently instructs `udevadm control --reload-rules`
unconditionally.

### `AGENTS.md` — add a lesson

Directly under the existing HID-permission caveat:

> **The udev remediation must be gated on udev actually running.** `udevadm control`
> talks to `/run/udev/control`; when that socket is absent (WSL without systemd,
> containers) the reload fails with "No such file or directory" and rules never fire.
> A real WSL user followed the printed block exactly and lost ~40 minutes. `TAG+="uaccess"`
> is additionally inert without a logind seat, which WSL has none of — hence the added
> `GROUP="plugdev"`. Branch on the capability (`/run/udev/control`), never on the platform
> name. **Never print a command that cannot work on the machine you are printing it to.**

---

## 11. Test requirements

`tests/` is 240 hardware-free, network-free tests under a second. Keep that property.

- **`tests/conftest.py` — extend the autouse subprocess rail.** It currently neuters
  `systemctl/launchctl/loginctl/openssl/git/uv/pip` (V13). Add `usbipd` **and**
  `usbipd.exe`. Without this, a careless test could attach a real USB device away from the
  developer's Windows host — a mutation of state *outside the machine running the tests*,
  which is a strictly worse blast radius than anything the rails guard today.
- **`tests/test_safety_rails.py`** — assert the two new entries, per the existing
  "fails loudly if a rail is weakened" contract.
- `test_usbnode.py` — fake sysfs tree in `tmp_path`; cover found / not-found / partial
  attrs / unreadable / mode & accessibility. Uses the injectable roots; no marker needed.
- `test_wsl.py` — `detect()` against fixture `osrelease` strings (WSL2 / WSL1 / native);
  `list_devices()` against captured `usbipd.exe list` text incl. the `Persisted:` section,
  an unknown state word, and empty output; `None` on timeout / `FileNotFoundError` /
  `OSError(8, "Exec format error")`.
- `test_hidhelp.py` — **table-driven over all states W0–W9, U-DEAD, U-LIVE, IMPOSTOR.**
  Assert on `Guidance.state` plus substring checks for the substituted BUSID and node path.
  One test asserts **no message anywhere contains the literal `<BUSID>` or `<NODE>`** when
  the value was resolvable (P5), and one asserts every printed `usbipd` occurrence is
  `usbipd.exe` (§6.4).
- `test_cli_doctor.py` — add: on W0 + `udev_live`, `explain_environment()` returns `[]` and
  doctor's output is unchanged (no-regression guard for macOS/Linux users).
- `test_main_logging.py` — 50 simulated open failures produce exactly **one**
  `logger.error` and **one** `exc_info=True` record; a changed error signature starts a new
  episode; the status file gets a `hint`.
- `test_cli_wsl_attach.py` — every state's exit code; asserts `attach()` is **not** called
  in W4 (not shared) and **is** called in W5.

---

## 12. Explicitly out of scope (rejected, with reasons)

| Rejected | Why |
|---|---|
| Native Windows (`sys.platform == "win32"`) support | Different problem entirely; nothing here applies |
| Any `sudo` invocation, even prompted | P3 — hangs under systemd, breaks piping, sudoers-dependent |
| Writing `/etc/udev/rules.d/*` or `/etc/wsl.conf` for the user | Breaks the repo's standing "never writes to /etc" promise (`service.py:219`) |
| A root **system** systemd unit (`User=root`) | Real option, but a service-model change with config/status-home consequences. Documented in `docs/WSL.md`; not built |
| Auto-installing usbipd-win via `winget` | Installing Windows software from a Linux CLI is a bridge too far |
| `usbipd.exe bind` / `detach` wrappers | `bind` needs elevation we refuse to obtain; `detach` isn't a cold-start need |
| A background re-attach watcher | A daemon that re-plugs USB devices across the Windows/Linux boundary is exactly the surprising mutation §6.2 argues against |
| Parsing `usbipd.exe state` JSON in v1 | Unverified schema (U4); the `list` format is evidenced. Hardening, not v1 |
| Using `StreamDeck`'s `device_info['path']` to find the node | hidapi-internal format, varies by release; sysfs is kernel-provided |
| `WSL_DISTRO_NAME` / `WSL_INTEROP` for detection | Shell-injected; absent under the systemd unit the sidecar runs in |
| Retitling `TransportError` upstream | Not our library. We add context around it |

---

## 13. Success criteria

Prove on the ALIENWARE WSL box, with a genuinely cold `uv tool install muxplex-deck`:

1. Deck unplugged from Windows → `muxplex-deck doctor` prints §7.3-family guidance naming a
   real BUSID (or §7.1/§7.2 as appropriate). **No `<BUSID>` placeholder anywhere.**
2. Deck plugged, not bound → `doctor` prints §7.2 with the real BUSID and the
   administrator-PowerShell instruction. `muxplex-deck wsl attach` exits 1 with the same text.
3. Bound, not attached → `muxplex-deck wsl attach` attaches and prints §7.8.
4. Attached, no permission → `doctor` and the `wsl attach` tail both name the exact node
   (`/dev/bus/usb/BBB/DDD`) and its mode. Running the printed `chown` verbatim, then
   `muxplex-deck service restart`, yields a working deck. **This is the end-to-end gate.**
5. Throughout, `service install` **never** prints the udev block while
   `/run/udev/control` is absent.
6. `journalctl --user -u muxplex-deck` over a 2-minute failing window contains **exactly one**
   traceback, not sixty.
7. `muxplex-deck status` during that window shows a **fresh** status file with the
   actionable hint, and does **not** claim the server is unreachable.
8. If U7 is confirmed: the systemd+udev+plugdev path survives a detach/re-attach with no
   `chown`. **If U7 is not confirmed, delete §7.5's durable-fix block** and ship the
   per-attach path only.
9. On macOS and on this Linux host, `doctor` output is byte-identical to v0.4.1's.
10. `uv run pytest` still green, still under a second, rails intact.

---

## 14. Open question for the requester

**Should `muxplex-deck wsl attach` exist as a subcommand, or should attaching happen only
inside `init`?**

My recommendation is the subcommand, and the deciding argument is not convenience — it is
that it removes the `usbipd` / `usbipd.exe` trap from the common path entirely (§6.4). A
user who never types the name cannot get the wrong binary. `init` runs once; re-attach
happens after every unplug and every Windows reboot, so the recurring path is the one that
needs to be safe.

The cost is one new subcommand group in a CLI that currently has ten. If that is too much
surface, the fallback is: `init` prompts, and every other command prints
`usbipd.exe attach --wsl --busid 1-4` with the real BUSID and an explicit note about the
`.exe`. That is meaningfully worse but still far better than today.
