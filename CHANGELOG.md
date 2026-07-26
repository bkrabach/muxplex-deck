# Changelog

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
