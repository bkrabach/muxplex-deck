# Changelog

## v0.9.4 (2026-07-28)

### Bug Fixes

- **Stopping the service now clears the Stream Deck's screen.** Pressing Ctrl+C on a foreground run already reset the device, because the sidecar could clean up after itself. Stopping the background service could not: every platform's service manager ultimately terminates the process outright — immediately on Windows, and after a timeout on Linux and macOS — which bypasses the program's own shutdown handling entirely. The result was a deck left displaying sessions it was no longer tracking. The stop command now clears the device itself, after first confirming the sidecar has actually exited so the two never compete for it. If that confirmation times out, the screen is deliberately left alone and the command says so rather than risking a fight over the device. A failure to clear the screen never turns a successful stop into a reported failure.

### Verification

- 656 tests passed via pytest (baseline 642 + 14 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.9.3 (2026-07-28)

### Bug Fixes

- **`service restart` no longer leaves the Windows service stopped.** Restarting asked the scheduler to end the task and then immediately asked it to start again, on the assumption that ending a task completes synchronously. It does not. The start request arrived while the scheduler still considered the old instance to be running, and because the task is configured to ignore a new instance while one is active, the request was silently discarded — leaving the old process gone, no new one started, and the task sitting idle. Restart now waits for the scheduler to report the task as no longer running before starting it again, and says so plainly if that wait times out instead of reporting a success it cannot confirm.

- **Selecting a session on Windows now raises the muxplex window instead of only flashing its taskbar button.** Windows refuses foreground changes requested by a background process, which is why the previous attempt could only flash the button. Microsoft documents that the restriction is lifted when the ALT key is pressed, so the sidecar now briefly signals ALT immediately before making the request. Nothing about the machine's settings is changed to achieve this. As before, the result is confirmed afterwards rather than assumed, and a failure to raise the window is recorded honestly.

### Verification

- 642 tests passed via pytest (baseline 629 + 13 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.9.2 (2026-07-28)

### Bug Fixes

- **`status` no longer reports a healthy Windows service as showing a previous run's data.** The process id used to identify the running sidecar came from the task scheduler, which reports the id of the scheduler's own host process — not the program the task launched. Status compared that against the id the sidecar records for itself, found two unrelated numbers, and concluded indefinitely that it was looking at stale data while everything was in fact working. On Windows the sidecar's own recorded id is now treated as the only authoritative source, and the check falls back to how recently the status was written. The guarantee added in v0.5.3 — that a restart never reports success until the newly started process has actually published its own status — is preserved by comparing against the id recorded before the restart began.

- **`service install` no longer appears to hang until a key is pressed.** Registering the task named the account to run as, and the Windows scheduling command always prompts for a password when given an account name, even for the current user on the local machine, and even when the task definition needs no password. The prompt was invisible because the command's input was still attached to the terminal, so installation simply appeared to stall until Enter was pressed. The account name is now omitted, since the task definition already carries the identity, and every Windows command the tool runs now has its input closed so that any future prompt fails immediately and visibly instead of hanging.

### Verification

- 629 tests passed via pytest (baseline 627 + 2 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.9.1 (2026-07-28)

### Bug Fixes

- **The `--log-file` option was accepted and then ignored.** Every run flag was parsed correctly and then dropped: the code that finally starts the sidecar never read the parsed value back out, so a log file was never opened. Running in a terminal hid this completely, because logging falls back to the console — but the background service on Windows runs without a console, so its log went nowhere and the service appeared to produce no diagnostics at all. A second, independent defect in the same area meant any run flag written *before* the `run` command was reset to nothing, a known behaviour of the argument parser when a subcommand re-declares the same options; flags now work written on either side of `run`.

- **The Stream Deck no longer keeps displaying the last screen after the sidecar stops.** Stopping the service left the final frame painted on the device indefinitely, showing sessions that were no longer being tracked. The device is now reset on the way out — on stop, on interrupt, and on unexpected errors. A forced kill still cannot be intercepted, and in that case the last frame will remain.

### Verification

- 627 tests passed via pytest (baseline 611 + 16 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.9.0 (2026-07-28)

### Features

- **Only one copy of the sidecar can run at a time.** Two copies competing for the same Stream Deck and the same status file produced a state that could not resolve: each wrote its own status, and `status` compared whichever wrote last against whichever the service manager reported, so it reported "waiting on the running process" indefinitely while the deck itself worked fine. A second copy now detects the first and exits immediately with a clear message, before touching the device or the server. The check asks the operating system directly rather than recording a process id in a file, so it clears the instant the first copy is gone by any means — clean exit, termination, or crash — and a restart is never blocked by a stale record.

- **On Windows, selecting a session brings the muxplex window to the foreground**, matching the behaviour that already existed on macOS — including pressing the already-selected session purely to raise the window. Windows can refuse a foreground change requested by a background process, and will sometimes report success while only flashing the taskbar button, so the result is confirmed after the fact and a failure is recorded honestly rather than reported as success.

### Bug Fixes

- **Mistyped commands now explain themselves in the same format as everything else, and suggest what you probably meant.** Argument errors were the one path still emitting raw parser output — a wall of usage text ending in a list of valid choices — which is exactly the moment a clear next step matters most. A close match is now offered as the action (`muxplex-deck server status` suggests `muxplex-deck service status`, keeping the rest of the command intact), and when nothing is close the fallback points at `--help` rather than inventing a suggestion.

- **The sidecar no longer dies silently when it cannot open its log file.** Opening the log was the first thing it did and was unguarded, so any failure there killed the process before a single line could be written — and with no console attached, that left no diagnostic anywhere at all. It now falls back through a plain file, then standard error, then no logging, rather than exiting.

### Verification

- 611 tests passed via pytest (baseline 572 + 39 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.8.0 (2026-07-27)

### Features

- **muxplex-deck can now run as a background service on Windows.** The sidecar previously had to be left running in a terminal; it can now be installed to start automatically. It registers as a scheduled task running as you, in your own session, which means no administrator rights are required and no password is stored anywhere — deliberately chosen over a conventional Windows service, which would have had to run either as the system account (whose home directory is the wrong place for your configuration and federation key) or as a named account whose password the service manager would need to keep. Two honest differences from the Linux and macOS services, stated by `service install` itself rather than left to be discovered: it starts when you log in rather than at boot, and after an unexpected exit it can take up to a minute to come back rather than a few seconds.

- **`service install`, `uninstall`, `start`, and `restart` now use the same verdict-and-next-action format as `doctor` and `status`.** These commands previously narrated progress line by line as they worked, in a different shape from the rest of the tool. They now report one verdict, the state, and — only when something needs doing — a single next action. Successful runs are quiet.

### Bug Fixes

- **`update` no longer fails on Windows when the service is running.** The update path stopped the running service by calling the Linux or macOS service manager directly, neither of which exists on Windows. This could not be reached before, because Windows had no service to detect; enabling background services on Windows would have made it fail on every update. Stopping the service now goes through the same platform dispatch as every other service command.

### Verification

- 572 tests passed via pytest (baseline 504 + 68 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.7.0 (2026-07-27)

### Features

- **Support for six-key, three-column Stream Decks.** On a deck this small, spending a corner key on the view selector and the opposite corners on paging leaves the controls scattered around the session keys. The bottom row is now reserved as a control row — previous page, view selector, next page, in that order — leaving the top row entirely for sessions. Layout selection continues to depend only on what the hardware reports about itself (key count, grid shape, dials, touchscreen) and never on a model name, so any three-column deck gets this arrangement.

- **`doctor` and `status` were redesigned around a verdict, a state list, and a single next action.** Previously every line carried the same weight, whether it reported a Python version that is never actionable or the one command the reader needed to run, and a healthy result was formatted identically to one with eight warnings. Output now opens with a one-line verdict that counts **actions rather than problems**, follows with the state, and ends — closest to the prompt — with the single thing to do. Items that cannot be evaluated until something upstream is resolved are shown as blocked rather than failed, so a missing configuration file reads as one action with several consequences instead of four independent problems. When there is nothing to do, the status markers disappear entirely: an empty margin means no work is needed, readable before any word is. A `--all` view expands every group into its members while keeping the same verdict and the same recommended action, so the shorter form hides evidence, never answers.

### Changes

- **Status readouts and health checks are now formatted differently.** Values like the active session and current view were being marked with a success indicator despite having no failure mode to indicate. They are now presented as readings rather than checks.

- **Multi-step remediation moved out of the state list.** Guidance that spans several lines — such as the Windows steps for sharing a device with WSL — now appears in the action section, so the state list stays one line per item regardless of how involved the fix is.

### Verification

- 504 tests passed via pytest (baseline 449 + 55 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.6.1 (2026-07-27)

### Bug Fixes

- **The federation key was still being saved without being checked.** v0.5.3 added a check that the key actually authenticates before writing it, but the check was making a request the server answers with a redirect rather than a yes or no, which the wizard read as "cannot verify" and then wrote the key regardless. The request now identifies itself as wanting a machine-readable answer, which is what the server needs in order to answer the authentication question at all instead of redirecting to a sign-in page. Following that redirect would have been worse than the original problem: the redirect leads to a page that returns success, so a client that followed it would have accepted every key, valid or not.

- **Setup no longer offers to install a background service on platforms that cannot run one.** On Windows, `init` finished by asking whether to install the service and then failed with an error when told yes — the same shape as the crash-loop removed in v0.5.2, where an action was offered that could not possibly succeed. Setup now checks for a supported service manager first, states plainly when background-service install is not available, and names what to run instead. The closing summary no longer recommends the command that just failed.

- **Diagnostics no longer report the absence of Unix tooling as a problem on Windows.** A missing `systemctl` was reported as though a Linux install were broken, rather than as background-service support not being available yet; and a missing `openssl` was reported as a warning even though Windows does not ship one, which is not something the reader can act on.

### Changes

- **The federation-key prompt was rewritten.** It previously paused mid-setup, described a file living on the server using a path formatted for the machine the reader was not on, offered three different ways to obtain the key, accepted either a pasted secret or a file path through a single hidden field, and included a command template containing a placeholder the wizard already knew the value of. It now shows one command, with the real server name filled in and written so it is runnable from where it is printed, and offers an explicit way to defer — which finishes the rest of setup, skips the service offer, and names the single command to run when the key is available. The certificate fingerprint is still shown for out-of-band verification but no longer occupies the position immediately before the prompt. The connection-security message leads with what it means rather than with a raw error; the underlying detail remains available in verbose mode.

### Verification

- 449 tests passed via pytest (baseline 433 + 16 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.6.0 (2026-07-27)

### Features

- **Native Windows support — the sidecar now drives a Stream Deck on Windows itself, without WSL.** Until now a Windows user had to bridge the device into WSL over USB/IP: sharing it from an administrator PowerShell, re-attaching after every unplug and every reboot, and granting access to a device node whose number changed each time. On native Windows the deck is simply a local device and none of that applies. Everything about talking to a muxplex server — the server URL, a private CA, the federation key — is unchanged and carries over as-is, so an existing configuration works on either side. **This release covers running the sidecar (`muxplex-deck run`); installing it as a background service on Windows is not yet supported and remains available on macOS and Linux only.**

- **HIDAPI is now included for Windows.** The Stream Deck library loads HIDAPI by name at runtime and Windows ships no such library, so a fresh install previously failed to find any HID backend at all. The official library is now included with the package, and both mechanisms Windows uses to locate it are set, so an unrelated copy already present on the system cannot silently take precedence. `doctor` reports which one actually resolved, and names both paths when something else is shadowing the included copy.

- **`doctor` now explains Windows-specific situations** — the Elgato Stream Deck application holding the device open, a device still attached to WSL, a missing HIDAPI, or a shadowed one. The device-attached-to-WSL guidance notes that after detaching, the Stream Deck keeps displaying whatever WSL last drew on it until it is physically unplugged and reconnected; Windows cannot claim it before that.

### Bug Fixes

- **The federation-key check no longer prints a `chmod` command on Windows**, where file permissions are governed by NTFS access control rather than POSIX mode bits and the command cannot do anything.

- **Writing the status file no longer fails on Windows when something is reading it at that moment.** The atomic replace used to publish status is rejected on Windows while another process holds the file open, which would have produced exactly the kind of stale-status false alarm removed in v0.5.3. The write now retries briefly before giving up.

### Verification

- 433 tests passed via pytest (baseline 404 + 29 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.5.3 (2026-07-27)

### Bug Fixes

- **`status` no longer reports a healthy device and server as broken immediately after a restart.** `service restart` returned as soon as the service manager accepted the command, before the newly started process had published any status of its own — so a `status` run in the next second read the *previous* process's final snapshot and reported a connected Stream Deck as disconnected and a reachable server as unreachable. Restart now waits for the running process to publish status under its own process id before reporting success, and says plainly when that has not happened within five seconds rather than claiming a success it cannot confirm. Independently, `status` now compares the process id recorded in the status file against the one actually running, and reports the device and server as *undetermined* rather than failed whenever the two disagree — so the same false alarm cannot appear after a crash-loop or a restart the tool did not itself perform. The check uses process identity rather than timestamp age deliberately: a dying process's last write can look recent while describing a system that no longer exists.

- **`init` no longer accepts a federation key without checking it.** The wizard wrote whatever key it was given, printed a reachability confirmation, and offered to install the service — and the reachability check ran against an endpoint that requires no credentials, so a wrong key produced a completely successful-looking setup that failed later as a service unable to authenticate. The key is now used to make one authenticated request before it is saved. A rejected key is reported at the prompt, is not written, and can be re-entered immediately. When the server cannot be reached to verify, that is stated as unverified rather than reported as success.

### Verification

- 404 tests passed via pytest (baseline 390 + 14 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.5.2 (2026-07-27)

### Bug Fixes

- **`service install` no longer starts a service that cannot run.** On a machine with no config yet, install would write the unit, enable it, and start it — at which point the sidecar exited immediately because it had nothing to connect to, and the restart policy put it straight into a loop. One user reached 1113 restarts before noticing. Install now verifies the service can actually start before enabling it, using the very same config load the installed unit performs at startup, so "ready to install" and "ready to run" cannot drift apart. When config is missing it stops, explains why, and names the command that creates it; re-running install afterwards completes the job. Applies to launchd as well as systemd.

- **`doctor` reported an installed service as "not installed."** The service check treated "not currently running" and "never installed" as the same condition, so a unit that was installed and failing was reported as absent — with a recommendation to install it again. Installed-but-not-running is now its own state, and it points at the logs rather than at a redundant reinstall.

- **The sidecar's own startup error pointed at the README.** When it could not find a config file it told the reader to consult the README for an example — in the one message read by someone who has just discovered config is missing. It now names the command that creates the config, matching the change made to `doctor` in v0.5.1.

### Verification

- 390 tests passed via pytest (baseline 374 + 16 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.5.1 (2026-07-27)

### Bug Fixes

- **The udev rule we printed could not be pasted.** The install command was emitted as a `<<'EOF'` heredoc indented for visual alignment, which put the terminator at a non-zero column — so pasting it left the shell waiting at a continuation prompt instead of writing the file. A one-line rule never needed a heredoc; it is now a single `echo … | sudo tee …` that survives paste regardless of surrounding indentation, and a test parses the emitted command with `bash -n` so this cannot silently regress.

- **That rule was also being offered on WSL, where it cannot work.** Install printed the udev remediation whenever udevd was running — but "udevd is running" does not mean "a rule will fire for a usbip-attached device," and on WSL it does not. A user could follow the instructions exactly and see the device node stay root-owned. The guidance is now withheld on WSL entirely, replaced by the per-attach ownership step that actually works there. The same wrong call existed in the setup wizard and is fixed by the same change.

- **`doctor` contradicted itself.** One check would locate the Stream Deck by BUSID and print precise instructions; the next would announce no Stream Deck was found and suggest checking the cable. Later checks now defer to what earlier ones established instead of re-reporting the device as missing.

- **One problem was reported as three.** An attached-but-unopenable device produced a detailed remediation block, a "detected" line, and a separate HID failure line — the last of which pointed at remediation that did not apply on that platform. It is one line now.

- **`doctor` pointed at the README** for the missing config file, in the output whose entire purpose is to avoid sending people to the docs. It names the command that creates the config.

- **`wsl attach` reported failure after succeeding.** It checked the Linux USB bus immediately after attaching and declared the device invisible when the bus had simply not settled. It now retries briefly before reporting a problem.

### Verification

- 374 tests passed via pytest (baseline 360 + 14 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.5.0 (2026-07-27)

### Features

- **The CLI now teaches WSL setup instead of assuming it.** A Stream Deck reaches WSL only over USB/IP, and every step of that — sharing the device from Windows, attaching it to the distro, granting access to the resulting device node — happens outside anything the sidecar previously knew about. A new user got a generic "device not found" and was left to discover the rest. Now `doctor`, `status`, `service install`, `init`, and the sidecar itself detect which of those steps is outstanding and print the specific next command, with real values substituted: the actual BUSID discovered from `usbipd.exe list`, the actual device node resolved from sysfs. A new `muxplex-deck wsl attach` wraps the attach step, which matters because attaching is not one-time — it must be repeated after every unplug and every Windows reboot, and it is exactly where the `usbipd` name collision bites. Diagnostics only ever *read* USB/IP state; the single command that mutates the Windows host's device topology is the one explicitly named for it. The tool never invokes `sudo` — privileged steps are printed for you to run.

- **`muxplex-deck wsl attach`.** Resolves the Stream Deck's BUSID itself and attaches it to the current distro, always invoking the Windows binary by absolute path so the identically-named Linux `usbipd` from `linux-tools-common` — which advertises kernel packages that do not exist for the WSL kernel — cannot be selected by accident.

### Bug Fixes

- **The udev remediation was wrong on WSL and is no longer offered there.** Install printed a udev rule unconditionally; on WSL the rule never fires, so a user could follow the instructions exactly and see no change. Remediation is now gated on whether udev is actually running — probed by the presence of its control socket rather than by guessing the platform — which also corrects the same bad advice inside plain Linux containers. The shipped rule additionally carried a `hidraw` line that could never apply, since the device is only ever opened through libusb, and a `uaccess` tag that requires a logind seat WSL does not have.

- **`status` reported a healthy server as unreachable.** When the sidecar failed to open the device it returned to the top of its loop without publishing status, so the status file froze at whatever it last held — showing a stale "unreachable" for a server that was fine. The failure path now publishes state along with a hint naming the actual blocking step.

- **The open failure no longer floods the journal.** A full traceback was logged on every poll cycle for as long as the device stayed unavailable. It is now logged once per failure episode, with the traceback at debug level and a periodic counting heartbeat.

### Verification

- 360 tests passed via pytest (baseline 267 + 93 new).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.4.1 (2026-07-27)

### Bug Fixes

- **`service start` and `service restart` no longer crash when the service is already running.** `_launchd_start()` ran `launchctl bootstrap` with `check=True` and no handling for the case launchd rejects: a job that is already loaded, which it reports as exit 5. That is the expected outcome of asking an already-running service to start, and it surfaced as an unhandled `CalledProcessError` traceback while `status` simultaneously reported the service healthy. Every sibling helper in the file already used `check=False` and reported state gracefully; start was the outlier. Bootstrap now runs through one shared helper: exit 0 confirms, exit 5 reports that the service was already running, and any other failure prints launchctl's own stderr and exits cleanly rather than raising. `restart` additionally waits for the asynchronous `bootout` to complete — polling for up to five seconds before re-bootstrapping, then proceeding with a warning rather than hanging — since the previous stop-then-start sequence raced launchd's teardown. `service install` carried the identical bug on its own bootstrap call and is fixed the same way. On Linux, `systemd start`/`restart` had the same defect with a different trigger: `systemctl` is idempotent against a running unit, but running either command *before* `install` produced the same raw traceback. Both platforms now behave equivalently. Success paths, previously silent, now print confirmation.

- **`update` no longer drags PyPI installs back to git.** The update command hardcoded `uv tool install --force git+<repo>`, with a comment explaining that no PyPI release existed so no version gate was possible — true when written, and false since v0.4.0 published. Meanwhile `doctor` classified a correct PyPI install as "unknown install source" and advised running `update`, which then reinstalled from git. A user who migrated to PyPI, ran `doctor`, and followed its advice was silently reverted. `update` now reads the same PEP 610 install-source detection `doctor` already used and upgrades in place: PyPI installs from PyPI, git installs from git, and editable installs are refused outright with an explanation. `doctor` reports up-to-date or update-available for PyPI installs like it already did for git, so its recommendation is no longer a trap. A version gate now skips the whole stop/reinstall/restart cycle when already current, with `--force` to override, and an unreachable PyPI degrades to attempting the upgrade rather than blocking.

- **`update` no longer destroys editable installs.** It had no editable guard at all, so running it from a development checkout would have force-reinstalled from git over the working tree.

### Verification

- 267 tests passed via pytest (baseline 254 + 13 new tests added for these fixes).
- All five CI jobs green on both pushes: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.4.0 (2026-07-26)

### Features

- **First PyPI release.** This package had only ever been installable with `uv tool install git+https://github.com/bkrabach/muxplex-deck.git`, which put upgrades on a path with a sharp edge: an install pinned to a tag reports "Nothing to upgrade" indefinitely, because `uv tool upgrade` resolves strictly within the recorded requirement. Getting a fix onto a machine in that state took a forced reinstall against the git URL. Published releases make `uv tool upgrade muxplex-deck` mean what it says. A `publish.yml` workflow now builds and publishes on any `v*` tag via OIDC Trusted Publishing, matching the sibling repo's release path.

- **Adopts the new `muxplex-client` library.** The 264-line hand-rolled httpx client is deleted in favor of `muxplex-client>=0.19.0`, published from the muxplex repo. The semantics this repo had been re-implementing — attention/bell state, the input key allowlist, capture-depth bounds — now have a single home, with a contract test in the server's own suite asserting the two implementations agree. All 242 tests pass against the published package with no test-body changes beyond the interface rename the adoption required.

### Internal

- **The repository is now public.**

### Verification

- 242 tests passed via pytest.
- All five CI jobs green on feature push and release push: Python 3.11/3.12/3.13, latest-deps, and ruff/pyright checks.
- Verified clean resolution via `uv sync` in an isolated directory (no muxplex sibling on path) — `muxplex-client==0.19.0` resolved from PyPI, not editable path.

### Dependencies

- muxplex-client >= 0.19.0 (new)
- streamdeck >= 0.9.5
- pillow >= 10.0.0
- pytest >= 8.0.0 (dev)

### License & Attribution

Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.3.0

### Bug Fixes

- **The Stream Deck key press now foregrounds the PWA on every explicit press.** The previous gate on "did this change the active session" contradicted the actual intended use: the button press *is* the request to bring the window forward. A user pressing an already-active session's key expected the PWA to pop to the foreground; instead nothing happened. The change is mechanical — removed the `changed = self.active_session != name` gate and the conditional wrap around `focus.focus_app()` — so every key press (same session or new session) triggers the focus operation. This is the intended behavior for a dedicated hardware control surface that serves both session selection and window management.

### Internal

- **CI enforcement and toolchain integration.** This release brings the repo's first enforced CI, surfacing the test suite, safety rails, and the ruff/pyright toolchain that every previous commit claimed to run but nothing gated. The 240-test suite (14 of which are meta-tests proving the safety rails stay in place) now runs on every push. Five CI jobs gate all code: Python 3.11/3.12/3.13 plus a latest-deps job (mirrors user install behavior), and a safety-rails meta-verification that confirms four-layer defense against test suite damaging the running service remains intact.

- **66 pre-existing lint violations resolved.** The first CI run against the accumulated code surfaced 66 ruff violations and 2 pyright type errors. All have been resolved. The linter and type checker now pass clean on every commit, making future drift visible immediately.

### Verification

- 240 tests passed via pytest (including 14 meta-tests pinning the test-safety rails).
- All five CI jobs green: Python 3.11/3.12/3.13, latest-deps, and safety-rails verification.
- Stream Deck hardware verified: MBP (Stream Deck+ 8 keys, 4 dials, touch strip) and ALIENWARE-R13 WSL (Stream Deck Original V2 `0fd9:006d` 15 keys, 3x5).

### Dependencies
- streamdeck >= 0.9.5
- pillow >= 10.0.0
- httpx >= 0.27.0
- pytest >= 8.0.0 (dev)

### License & Attribution
Built with [Amplifier](https://github.com/microsoft/amplifier)

## v0.2.0

### Major Features

**Full CLI Surface**
- `muxplex-deck doctor` — Hardware diagnostic tool with capability detection
- `muxplex-deck status` — Query running sidecar status from an atomic status file (respects HID exclusivity)
- `muxplex-deck config` — View and manage configuration
- `muxplex-deck service` — Install/uninstall systemd (Linux) or launchd (macOS) service
- `muxplex-deck update` — Check for and apply updates

**Service Management**
- Systemd integration on Linux with standard service install/uninstall
- launchd integration on macOS with plist-based service management
- Graceful service lifecycle handling and dependency management

**Device I/O Improvements**
- Atomic status file writes — since HID devices grant exclusive access to one process, the status file must be written atomically so a monitoring process can read it without blocking the service
- Capability-driven probe that adapts to deck model — dynamically detects `is_visual`, `touch_key_count`, `vendor_id`, `product_id` for both standard and custom deck configurations
- Fixed `RealDeckDevice` missing the four required capability methods (`is_visual`, `touch_key_count`, `vendor_id`, `product_id`) — `doctor` no longer crashes when a physical deck is attached
- Stream Deck support verified across two hardware models (MBP Stream Deck+, Windows Stream Deck Original V2 `0fd9:006d`)

**Test Safety Rails**
- Four-layer defense against test suite damaging the running service:
  1. `pytest_sessionstart` guard — refuses to run against a live host, names the port and override method
  2. Autouse `SETTINGS_PATH` redirection to tmpdir — tests cannot reach the real config
  3. Autouse killer neutering — HID operations are no-ops unless marked with `@pytest.mark.allow_real_hid`
  4. `test_safety_rails.py` with 14 meta-tests proving the rails stay in place
- Comprehensive documentation of the incident (deleted tests killed production) and the permanent guards
- Explicit markers for intentional real-subprocess and real-HID tests so reviewers can identify risky operations

### Resolved Issues
- Service integration now prevents double-reporting of the sidecar's own service as an HID failure
- Deck capability detection no longer crashes on missing methods
- Test suite can no longer inadvertently SIGTERM a running muxplex instance

### Dependencies
- streamdeck >= 0.9.5
- pillow >= 10.0.0
- httpx >= 0.27.0
- pytest >= 8.0.0 (dev)

### License & Attribution
Built with [Amplifier](https://github.com/microsoft/amplifier)
