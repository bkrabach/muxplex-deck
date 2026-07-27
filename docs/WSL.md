# muxplex-deck on WSL2

**You should not need this document.** `muxplex-deck doctor`, `muxplex-deck
init`, `muxplex-deck service install`, and `muxplex-deck wsl attach` are
designed to walk a cold-start WSL2 user to a working Stream Deck entirely
through their own printed output -- copy-pasteable commands with real
values (BUSIDs, device nodes) substituted in, not placeholders. This page
exists for depth the CLI shouldn't carry: full annotated walkthroughs,
trade-offs, and the handful of things that are genuinely easier to explain
in prose than in a terminal.

## Why this exists at all

WSL2 does not give Linux direct access to USB devices. Windows owns the
USB stack; a device has to be explicitly handed over to the WSL VM via
[usbipd-win](https://github.com/dorssel/usbipd-win)'s USB/IP bridge before
Linux can see it at all. Once Linux can see it, a *second*, unrelated
problem shows up: by default, only `root` can open a raw USB device node,
so a systemd **user** service (running as you, not root) still can't open
the Stream Deck without an access grant of some kind.

Two different problems, two different fixes. `muxplex-deck wsl attach`
solves the first. The `sudo chown`/udev-rule guidance solves the second.

## The full walkthrough

### 1. Install usbipd-win (one time, on Windows)

In any Windows PowerShell (admin not required for this step):

```powershell
winget install --interactive --exact dorssel.usbipd-win
```

### 2. Share the device with WSL (one time per device, needs admin)

Plug the Stream Deck into the PC, then in an **elevated** (Run as
Administrator) PowerShell:

```powershell
usbipd list
```

Expected output (ground truth from a real session):

```
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-4    0fd9:006d  Elgato Stream Deck                                            Not shared
```

Bind it (elevation required -- this is a one-time step per device, not
per boot):

```powershell
usbipd bind --busid 1-4
```

**Close the official Elgato Stream Deck app first.** It holds the device
open; `bind`/`attach` can fail or misbehave while it's running.

### 3. Attach it to WSL (every time you plug it in, or reboot Windows)

Back inside WSL, no elevation needed:

```sh
muxplex-deck wsl attach
```

This finds the device, checks whether it's shared, attaches it, resolves
its Linux device node from sysfs, and tells you exactly what's left (a
`chown` if the node still isn't accessible). Expected output once
everything downstream is also fine:

```
muxplex-deck wsl attach

  ✓ WSL2 detected (5.15.90.1-microsoft-standard-WSL2)
  ✓ usbipd.exe: /mnt/c/Program Files/usbipd-win/usbipd.exe
  ✓ Found on Windows: BUSID 1-4  0fd9:006d  Elgato Stream Deck
  ✓ Shared -- attaching...
  ✓ Attached
  ✓ Visible to Linux: /dev/bus/usb/001/003

  Next:
    muxplex-deck service restart
    muxplex-deck status

  The device number changes on every attach -- re-run `muxplex-deck wsl attach`
  after any unplug or Windows reboot.
```

### 4. Grant this user access to the node (per-attach, until you pick a durable option)

If `wsl attach` reports the node isn't openable yet, it prints the exact
command -- something like:

```sh
sudo chown "$(id -un)" /dev/bus/usb/001/003
muxplex-deck service restart
```

`chown` (rather than `chmod 666`) is what the CLI recommends: same
durability, strictly less exposure (only you get access, not every local
user), and mechanically correct either way -- the node comes up owned by
`root:root` with owner read/write bits set, so taking ownership grants
you both.

`chmod 666 /dev/bus/usb/BBB/DDD` is the equivalent alternative if you'd
rather not change ownership -- it works exactly as well, just more openly.

**Heads up: the device number (the `DDD` in `BBB/DDD`) changes on every
attach.** Whatever fix you apply is per-attach, not permanent, unless you
take the durable path below.

## Durable options (survive re-attach) -- honest trade-offs

| Option | Survives re-attach | Cost | Verified on this project |
|---|---|---|---|
| systemd + udev rule + `plugdev` group | Yes, if it works on your setup | `wsl.exe --shutdown` once, log out/in once for group membership | **Not proven end-to-end on a real WSL box as of this writing** -- see caveat below |
| Per-attach `sudo chown` | No | One command, once per attach | Yes -- this is the ground-truth path that got a real user's deck working |
| Run the sidecar under `sudo` | Yes | No systemd **user** service (you lose `service install`'s per-user model); config paths become sudo-aware | Yes -- this is what worked on the ALIENWARE box in this project before `wsl attach` existed |

### Why the "durable" systemd+udev option is documented, not automated

`muxplex-deck` will turn on systemd for you *only as a printed command* --
never by writing `/etc/wsl.conf` itself (the tool never writes to `/etc`,
on any platform, WSL included). If udev isn't running
(`/run/udev/control` absent -- true for most WSL distros without
`systemd=true`), `doctor`/`wsl attach` will tell you so and print:

```sh
printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
```

Then, in Windows PowerShell:

```powershell
wsl.exe --shutdown
```

Reopen the distro and run `muxplex-deck wsl attach` again. If udev is now
live, `doctor` will offer the udev-rule remediation for a device that
grants access on every attach without a manual `chown`:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

Also make sure you're actually in the `plugdev` group (then log out and
back in for it to take effect):

```sh
sudo usermod -aG plugdev "$(id -un)"
```

**The honest caveat:** whether a udev rule reliably fires for a
USB/IP-attached device on your specific WSL2 build has not been proven
end-to-end by this project as of this writing -- the person who wrote
this guidance does not have a WSL box to test it on. If it works for you,
great -- it's genuinely the least-friction long-term answer. If it
doesn't, the per-attach `sudo chown` (or running the sidecar under `sudo`)
works unconditionally and is what's actually been verified. **Never treat
the udev+systemd path as guaranteed** -- if `wsl attach` still reports "not
accessible" after doing all of the above, fall back to the per-attach fix
without further troubleshooting of the udev path; it may simply not work
on your build yet.

## `usbipd` vs `usbipd.exe` -- a real trap, and how this tool avoids it

If you've installed WSL's optional Linux USB/IP tooling (`sudo apt install
linux-tools-common linux-tools-generic` or similar), your PATH may resolve
a bare `usbipd` to the **Linux** USB/IP daemon -- a completely different
program from the Windows bridge. Running `usbipd list` in that case
produces something like:

```
$ usbipd list
usbipd: command not found: modprobe usbip-core
```

or a complaint about a missing `linux-tools-<kernel>` package -- **a
package that does not exist for the WSL kernel.** You did nothing wrong;
you just ran the wrong program.

Three layers protect you from this:

1. **`muxplex-deck wsl attach` never types either name for you.** Use it
   and you never risk this at all.
2. **Every command this tool prints spells out `usbipd.exe`, in full,
   everywhere** -- including inside an elevated PowerShell block, where
   the `.exe` is technically redundant. Consistency here is worth more
   than three characters.
3. **If both binaries are actually on your PATH**, `doctor`/`wsl attach`
   detect it and print an explicit disambiguation naming both real paths
   -- this only fires when it's actually true, so it's not noise for the
   majority of users who never installed the Linux tooling.

## Interop disabled / "Exec format error"

If WSL's Windows-interop feature is switched off for your distro (rare,
usually a deliberate `/etc/wsl.conf` setting), trying to run `usbipd.exe`
from inside WSL fails immediately with something like:

```
OSError: [Errno 8] Exec format error: '/mnt/c/Program Files/usbipd-win/usbipd.exe'
```

`muxplex-deck` treats this identically to "usbipd.exe not found" -- same
guidance, because the practical fix is the same: either re-enable
interop (`/etc/wsl.conf`'s `[interop]` section, `enabled = true`) or run
the Windows-side `usbipd.exe` commands directly from PowerShell instead of
through this tool.

## WSL1: not supported

USB/IP device passthrough is a WSL2 feature. If `muxplex-deck` detects
WSL1 (from `/proc/sys/kernel/osrelease`, which contains `Microsoft`
without `WSL2` on WSL1), every command fails fast with a message pointing
you at upgrading:

```powershell
wsl.exe --set-version <distro-name> 2
```

(`wsl.exe -l -v` lists your distros and their current version.)

## BUSID and device-number churn -- what actually changes and when

- **BUSID** (e.g. `1-4`) is assigned by *which physical USB port* the
  device is plugged into on the Windows host. Move it to a different
  port and the BUSID changes. This is why `muxplex-deck doctor`/`wsl
  attach` always re-query `usbipd.exe list` rather than caching a BUSID
  from a previous run.
- **The Linux device number** (the `DDD` in `/dev/bus/usb/BBB/DDD`)
  changes on **every** `wsl attach` -- even to the same port -- because
  it's assigned fresh by the kernel each time the device is attached.
  This is why the permission fix (`chown`/`chmod`) is inherently
  per-attach unless you take the durable systemd+udev path above.

## Why detection is capability-based, not "if WSL"

If you read `hidhelp.py`/`usbnode.py`'s source: the permission remediation
branches on whether `/run/udev/control` exists (i.e. "will a udev rule
ever fire here"), not on "is this WSL." This is deliberate -- it means the
exact same fix also repairs a plain Linux container or minimal image
without systemd/udev, and it won't need updating if a future WSL release
ships udev by default. WSL-specific knowledge (usbipd, BUSIDs, `wsl
attach`) is layered in only where nothing else could possibly supply it.
