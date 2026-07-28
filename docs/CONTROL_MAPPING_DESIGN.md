# Control Mapping Design — user-configurable actions per control

**Status:** DESIGN ONLY. No code written. Awaiting decisions in
["Open questions"](#open-questions).
**Target:** muxplex-deck v0.9.4 (`main`, clean, 656 tests).
**Ask:** *"make it user configurable which actions/behaviors to assign to each
control (button/screen, dial/push, touch screen, etc.), with a list of
available options to choose from for each and a way to set it up however a
given user wants (keeping our current as the defaults)."*

---

## 0. The one constraint that shapes everything

`AGENTS.md` § *"Capability-driven, never model-name-driven"*: the sidecar
branches only on `key_count`, `key_layout`, `dial_count`, `is_touch` — never
on `deck_type()`. Any design that needs a per-model table is disqualified
before it is evaluated.

That rules out the obvious approach (named profiles per deck model) and
points at the one that survives: **bind actions to *capability-space
addresses* — `key.N`, `dial.N.turn`, `dial.N.push` — which every deck
self-describes through the capability accessors already in use.** A binding
is applicable if and only if its index is within the deck's own reported
count. Nothing needs to know what the deck is called.

Second constraint, from the same file and from five separate incidents on
2026-07-28: **configuration must never be silently discarded.** Every
binding either takes effect, or is reported by name with a reason.

---

## 1. Ground truth — what the controls do today

Derived by reading the code, not from the README.

### 1.1 Dispatch sites

| Control | Today's behavior | Code |
|---|---|---|
| key press (FULL, all keys) | connect the session in that slot | `main.py:776` → `main.py:800` → `connect_slot` `main.py:802` |
| key press (REDUCED, VIEW key) | open the paged view picker | `main.py:777-785` |
| key press (REDUCED, PREV key) | `pager.turn(-1)` | `main.py:786-790` |
| key press (REDUCED, NEXT key) | `pager.turn(+1)` | `main.py:791-795` |
| key press (REDUCED, other keys) | connect the session in that slot | `main.py:797-800` |
| dial 0 turn | `ViewCycler.turn` — debounced `PATCH active_view` | `main.py:714` |
| dial 0 push | toggle the VIEW picker | `main.py:717-719` |
| dial 1 turn | `Pager.turn` — local paging, no server write | `main.py:744` |
| dial 1 push | toggle the PAGE picker | `main.py:747-749` |
| dial 2, dial 3 (Deck+) | **nothing** — logged as `(unassigned)` | `main.py:942-943` |
| touch strip | **nothing** — `_on_touch` logs `(unassigned in v1)` | `main.py:948-949` |
| any dial (REDUCED deck that *has* dials) | **nothing** — `use_dials=False` | `layout.py:202`, `main.py:1005-1008` |

Three of the Deck+'s thirteen controls do nothing. On a dial-having,
touchless deck, *every* dial does nothing.

### 1.2 Where the geometry is decided

`layout.plan_layout` (`layout.py:142-204`) picks FULL vs REDUCED from
`dial_count >= 2 and is_touch` (`layout.py:158`), then
`_reserved_control_keys` (`layout.py:121-139`) picks the three control key
positions from grid shape alone. `classify_key` (`layout.py:207-227`) maps a
physical key back to its role. `LayoutPlan` (`layout.py:100-118`) is the one
decision object.

Consumers of `plan.view_key` / `prev_key` / `next_key` outside `layout.py`,
all painting or mode-gating — this is the full blast radius of a shape
change:

- `main.py:336` — status-message key placement on strip-less decks
- `main.py:511`, `main.py:767` — "is this the reduced picker?" gate
- `main.py:608-610` — picker-mode control labels (BACK / PREV / NEXT)
- `main.py:685-687` — normal-mode control labels (VIEW / PREV / NEXT)

### 1.3 Two dead functions that this feature revives

`ViewCycler.press` (`interaction.py:162-171`, "jump to all") and
`Pager.press` (`interaction.py:210-213`, "jump to page 1") have **zero
callers** in `src/` or `tests/` — verified by grep. They were the dial-press
behaviors before the picker replaced them (`interaction.py:249-253` records
the swap). Today they are dead code, which
`LANGUAGE_PHILOSOPHY.md` §6 correctly calls context poison.

They are also, exactly, two useful bindable actions. This design promotes
them back to live code rather than deleting them. **If the decision is not
to ship this feature, delete them instead** — leaving them unreachable is
the one option that is wrong either way.

### 1.4 Focus is a side effect, not a control

`focus.focus_app` fires inside `_do_connect` (`main.py:849`) on every
key-press connect. It is not independently addressable. See §11 for why it
is proposed as a Tier-3 action rather than a Tier-1 one.

---

## 2. The action catalog

### 2.0 Kinds — the two-kind split, re-examined and kept

Each action has a **kind** that determines which addresses can carry it:

- **momentary** — fires on a discrete press. Valid on `key.N` and
  `dial.N.push`.
- **relative** — consumes a signed tick count. Valid on `dial.N.turn` only.

The two sets are disjoint. `page_next` on a dial turn is nonsense (a turn
supplies ±ticks, not a direction); `view_cycle` on a key is nonsense (a
press supplies no ticks). Binding across kinds is a hard config error
(§6, Gate 1) — not a silent coercion.

**Does a wider catalog break this?** No — and the widening actually
*confirms* it. Every continuous quantity the deck can drive turns out to
have the same two-form shape, and there are now three instances of it:

| Axis | relative form (dial turn) | momentary pair (key / dial push) |
|---|---|---|
| view | `view_cycle` | `view_prev` / `view_next` |
| page | `page_cycle` | `page_prev` / `page_next` |
| brightness | `brightness_cycle` | `brightness_up` / `brightness_down` |

Brightness was the candidate most likely to force a third kind — it is
"naturally relative." It doesn't: it decomposes into exactly the same pair
the other two axes already use. A third kind would have to be justified by
an action that is *neither* discrete nor tick-driven. The two real
candidates are **absolute** (a touch-strip tap at position *x* meaning
"set the value to *x*") and **held** (long-press). Neither is in this
catalog, and both arrive — if ever — with touch bindings (§11.4). Note it
as the likely trigger; do not build for it now.

Verified: `DialEventType.PUSH` carries a truthy/falsy `value` and is
handled as a discrete press (`main.py:717`, `main.py:747`) — dial push is
genuinely momentary, not a third thing.

### 2.1 Tier 1 — pure remaps of existing behavior

| Action | Kind | Behavior | Existing implementation |
|---|---|---|---|
| `session` | momentary | Connect the session shown in this slot. **Default for any key with no other binding.** | `main.py:802` `connect_slot` |
| `view_picker` | momentary | Open/close the paged view picker | `interaction.py:280-287` `press_view_dial` |
| `page_picker` | momentary | Open/close the page picker | `interaction.py:289-296` `press_page_dial` |
| `page_prev` | momentary | Page −1 (clamped) | `interaction.py:205-208` `Pager.turn(-1)` |
| `page_next` | momentary | Page +1 (clamped) | `interaction.py:205-208` `Pager.turn(+1)` |
| `none` | momentary | Unassigned. Key paints blank, press logs and ignores | `main.py:797-799`, `main.py:943` |
| `view_cycle` | relative | Debounced `PATCH active_view` by ticks | `interaction.py:135-152` `ViewCycler.turn` |
| `page_cycle` | relative | Local paging by ticks | `interaction.py:205-208` `Pager.turn` |

### 2.2 Tier 2 — reachable machinery that nothing currently calls

| Action | Kind | Behavior | Existing implementation | New |
|---|---|---|---|---|
| `view_all` | momentary | Jump straight to the `all` view, no debounce | `interaction.py:162-171` `ViewCycler.press` (**0 callers**) | ~3 |
| `page_first` | momentary | Jump straight to page 1 | `interaction.py:210-213` `Pager.press` (**0 callers**) | ~3 |
| `page_last` | momentary | Jump to the final page | `interaction.py:215-219` `Pager.go_to` | ~3 |
| `view_prev` | momentary | Step one view back | `interaction.py:135-152` `ViewCycler.turn(-1, commit)` | ~4 |
| `view_next` | momentary | Step one view forward | `interaction.py:135-152` `ViewCycler.turn(+1, commit)` | ~4 |
| `focus_app` | momentary | Bring the muxplex PWA to the foreground | `focus.py:52` `focus_app` | ~8 |
| `refresh_now` | momentary | Poll the server immediately | `main.py` `_ActiveRuntime.refresh` | ~8 |
| `toggle_last` | momentary | Connect the previously-active session | `main.py:802` `connect_slot` + 1 new tracked field | ~20 |
| `brightness_up` | momentary | Brightness +10% (clamped) | `device.py:78` `set_brightness` | ~10 |
| `brightness_down` | momentary | Brightness −10% (clamped) | `device.py:78` `set_brightness` | (shared) |
| `brightness_cycle` | relative | Brightness by ticks (clamped) | `deck_probe/events.py:126-130` — **already implemented in the PoC** | ~15 |

### 2.3 Ranked by value ÷ lines — the evaluation

Ordered best-first. "Lines" is dispatch + logic only; per-action catalog
entry, help text, README row, and test are a further ~20 each and are
counted once in §9.

| # | Action(s) | Verdict | Lines | Why |
|---|---|---|---|---|
| 1 | `view_prev` / `view_next` | **SHIP** | ~8 | Highest ratio in the catalog. On a REDUCED deck there is **no way to step views at all** today — only the picker (open, page, tap). This is the exact twin of `page_prev`/`page_next` and reuses `ViewCycler.turn` unchanged, debounce included: tapping next-next-next collapses to **one** PATCH (`interaction.py:144-151`). Observability is free — the VIEW key already paints the view name (`main.py:685`). |
| 2 | `page_last` | **SHIP** | ~3 | `Pager.go_to` already exists and is already called by the page picker (`main.py:922`). Pairs with `page_first`. Three lines. |
| 3 | `focus_app` | **SHIP** — *reversing §11.6* | ~8 | `focus.focus_app(name)` is one call and already fires at `main.py:849`. Fully implemented, proven on macOS. **Reversal noted honestly:** v1 deferred this as "new capability bundled into a compatibility feature." With low-hanging fruit now the explicit goal, that reasoning no longer holds — the cost is 8 lines of proven code. It is also the *only* mitigation available for the Windows service-context focus failure: a dedicated key lets the user raise the PWA on demand when auto-focus can't. Advisory (§6) if `focus_app` config is empty, since the action would silently no-op (`focus.py:58-59`). |
| 4 | `refresh_now` | **SHIP** | ~8 | Genuinely a primitive, **and it needs no poll-loop surgery** — see §2.4. |
| 5 | `brightness_*` (3 actions) | **SHIP** | ~30 | Best answer to the Deck+'s dead dials 2/3, and the clamp-and-set logic is already written and working in `deck_probe/events.py:126-130`. Perfectly observable: the state *is* the display. See §2.5 for the persistence decision. |
| 6 | `toggle_last` | **SHIP** (borderline) | ~20 | Alt-tab is a top-tier workflow verb. Nothing tracks previous today — `self.active_session` is written in `_process` and `connect_slot` with no history (`main.py:362-390` field list). One new field, updated in one place, plus a dead-session guard. Self-observing: the highlight moves. |
| 7 | `sort_toggle` | **DEFER** | ~15 | Cheap to execute, but fails the observability constraint. See §11.10. |
| 8 | `display_toggle` (blank/wake) | **KILL** for v1 | ~10 *nominal* | Sounds trivial, isn't. See §11.11. |
| 9 | `view_hidden` | **DEFER** | ~3 | The thin end of the parameterization wedge. See §11.12. |

### 2.4 `refresh_now` — why it needs no poll-loop restructuring

The obvious implementation is to interrupt `_interruptible_wait`
(`main.py`, 1-second tick loop over `shutting_down.wait(step)`) with a
second Event. That would work but touches the loop's return-value contract
("True = abandon this session"), which is load-bearing for unplug and
shutdown responsiveness.

Unnecessary. **Calling `refresh()` from a non-poll thread is already an
established, shipping pattern in this codebase:** `_commit_view` does
exactly that at `main.py:727`, from the debounce-timer thread and from
`_select_view_option`. `refresh()` serializes its HTTP through
`client_lock` and `_process` takes `paint_lock`, so a concurrent poll and
a manual refresh are both safe and idempotent — both write the same
server-derived state, last-writer-wins.

So `refresh_now` is: spawn a daemon thread calling `ctx.refresh()`, exactly
as `connect_slot` spawns `_do_connect` (`main.py:822`). The poll loop is
untouched. **~8 lines, zero restructuring.** This is the finding that moved
it from "probably needs the loop reworked" to a confident ship.

### 2.5 `brightness_*` — the two decisions it forces

**Persistence: NO.** `main.py:991` deliberately asserts
`FULL_BRIGHTNESS_PERCENT` on *every* bring-up, because real hardware powers
on at a dim firmware default (`main.py:988-990` comment). Persisting a
user's dimmed brightness to `config.json` would fight that deliberate
decision and could leave a deck that looks dead after a replug, with the
cause stored invisibly in a file. Brightness is therefore **session-local**:
it resets to 100% on every reconnect. Stated in `controls actions` output
and the README so it is never a surprise.

**Floor: 10%, not 0%.** `deck_probe` clamps to `0` (`events.py:127-129`),
which is correct for a hardware probe and wrong here — a user who binds
`brightness_down` to a key can walk it to a black screen and then cannot
see which key restores it. Clamping the *bound action* to a 10% floor keeps
the deck always readable. (0% remains reachable programmatically; it is
simply not reachable by holding down a bound key.) This is the same footgun
that kills `display_toggle` in §11.11 — the difference is that a floor
costs one `max()` and fully removes it here.

Observability: **the screen physically changes brightness.** No indirection,
no stale-state risk. This is the most directly observable action in the
catalog.

### 2.6 Not in the catalog, by construction

**Picker-mode meanings are derived, not bound.** While a picker is open the
controls take picker meanings *derived from their normal-mode binding*
(§7). There is no second binding table for picker mode, and no
`picker_scroll` / `picker_select` action — those would be a second
configuration surface describing a modal overlay the user never sees as
separate.

New actions inherit picker behavior from §7's table by kind: every
momentary action not in {`view_picker`, `page_picker`, `page_prev`,
`page_next`, `session`} is `ACTION_IGNORE` while a picker is open. That
covers `focus_app`, `refresh_now`, `toggle_last`, `brightness_*`,
`view_prev`/`view_next`, `page_last` with **no new rules** — the §7 table
was written as a default-deny and needs no edit.

---

## 3. Address grammar

```
address := "key" "." index
         | "dial" "." index "." ( "turn" | "push" )

index   := decimal integer, no sign, no leading zeros:  0 | [1-9][0-9]*
```

Examples: `key.0`, `key.14`, `dial.0.turn`, `dial.3.push`.
Rejected at Gate 1: `key.00`, `key.-1`, `key.1.press`, `dial.0`,
`touch.tap`, `KEY.0`, `key.0 ` (trailing space).

The grammar deliberately has no model names, no layout-mode names, and no
role names. An address is a coordinate in the capability space the deck
already reports through `key_count()` / `dial_count()`.

**Touch is not in the grammar.** Not an oversight — see §11.4. Adding
`touch.tap` later is a purely additive grammar extension requiring no
migration of any existing config.

---

## 4. Config schema

One new key in `DEFAULT_CONFIG` (`config.py:38-45`):

```python
"controls": {},   # address -> action; empty means "all defaults"
```

Three states per control, and the distinction between the second and third
is load-bearing:

| Config | Meaning |
|---|---|
| address absent | use the capability-derived default for this deck |
| `"key.0": "session"` | override: this key is a session tile |
| `"key.0": "none"` | override: this control does nothing at all |

### 4.1 Default — a fresh install, unchanged behavior

```json
{
  "server_url": "https://spark-1:8088",
  "key_file": "~/.config/muxplex-deck/federation_key",
  "ca_file": "~/.config/muxplex/ca/muxplex-ca.crt",
  "poll_interval": 2.0,
  "sort": "attention",
  "focus_app": "muxplex"
}
```

No `controls` key. Every existing config file on disk today is already
valid and produces byte-identical behavior.

### 4.2 The implicit default table (what the planner computes)

Shown for documentation only — never written to disk. This is the current
behavior expressed in the new vocabulary, which is the proof the model
covers what exists with no gaps.

**FULL** (Deck+ — `dial_count >= 2 and is_touch`):

```
key.0 .. key.7    session          (all keys are tiles)
dial.0.turn       view_cycle
dial.0.push       view_picker
dial.1.turn       page_cycle
dial.1.push       page_picker
dial.2.turn/push  none
dial.3.turn/push  none
```

**REDUCED, 15-key 3×5 Original** (corner geometry, `layout.py:139`):

```
key.0             view_picker
key.10            page_prev
key.14            page_next
all other keys    session         (12 slots/page)
every dial        none            (matches today's use_dials=False)
```

**REDUCED, 6-key 3×2 Mini** (bottom-row geometry, `layout.py:136-138`):

```
key.3             page_prev
key.4             view_picker
key.5             page_next
key.0 .. key.2    session         (3 slots/page)
```

### 4.3 One-key override — move VIEW off the top-left corner

```json
{
  "server_url": "https://spark-1:8088",
  "controls": {
    "key.0": "session",
    "key.4": "view_picker"
  }
}
```

Result on the 15-key deck: key 0 becomes a session tile, key 4 becomes
VIEW. `session_slots` recomputes automatically to
`(0,1,2,3,5,6,7,8,9,11,12,13)` and `sessions_per_page` stays 12. PREV/NEXT
are untouched — they were never mentioned.

### 4.4 Reclaiming the Deck+'s dead dials

```json
{
  "controls": {
    "dial.2.turn": "page_cycle",
    "dial.2.push": "page_first",
    "dial.3.push": "view_all"
  }
}
```

Dials 0 and 1 keep their defaults. This is the case with the clearest
payoff and it costs the user four lines.

### 4.5 Full remap — a REDUCED deck driven entirely from the bottom row

```json
{
  "controls": {
    "key.0":  "session",
    "key.10": "session",
    "key.14": "session",
    "key.11": "page_prev",
    "key.12": "view_picker",
    "key.13": "page_next"
  }
}
```

Every default control key is reclaimed as a tile; the three controls move
to the middle of the bottom row.

---

## 5. Merge model

Defaults are **computed, not stored**. The merge is a single dict overlay
inside the pure planner:

```
resolved = default_bindings(caps) | user_controls_applicable_to(caps)
```

- No config file ever contains a default value it didn't ask for.
- `muxplex-deck controls reset` deletes the `controls` key; behavior
  returns to whatever the *current* deck's defaults are — including any
  future improvement to `_reserved_control_keys`. A user who never
  configured a control never gets frozen on an old geometry.
- `config.py`'s existing `patch_raw_config` (`config.py:278-285`) already
  does whole-value replacement of known keys, which is correct here:
  `controls` is replaced wholesale by the `controls` subcommands, which
  read-modify-write the sub-dict themselves.

### `LayoutPlan` shape change

```python
@dataclass(frozen=True)
class LayoutPlan:
    mode: str
    key_count: int
    bindings: Mapping[str, str]        # NEW: resolved address -> action, every control
    session_slots: tuple[int, ...]     # DERIVED: keys whose action == "session", ascending
    sessions_per_page: int             # DERIVED: len(session_slots)
    use_dials: bool                    # DERIVED: any dial.* address resolves to non-"none"
    use_strip: bool                    # UNCHANGED: is_touch (the strip is a display, not a control)
    unapplied: tuple[Unapplied, ...]   # NEW: Gate-2 diagnostics (§6)

    # Convenience, first-match-by-ascending-index, or None. Kept so the six
    # existing consumers (§1.2) need no rewrite. Documented as "first" because
    # binding two keys to page_next is legal and both work.
    @property
    def view_key(self) -> int | None: ...
    @property
    def prev_key(self) -> int | None: ...
    @property
    def next_key(self) -> int | None: ...
```

`view_key`/`prev_key`/`next_key` become derived properties rather than
stored fields. `_reserved_control_keys` (`layout.py:121-139`) survives
verbatim — it now computes the *default binding table* instead of a tuple
of reserved indices. Its grid-shape reasoning, including the 3-column
special case, is unchanged.

`classify_key(plan, key)` returns `(action_name, slot | None)` instead of
`(KEY_VIEW|KEY_PREV|KEY_NEXT|KEY_SESSION, slot)`. The four `KEY_*`
constants are replaced by the catalog names — a rename across three call
sites (`main.py:776`, `main.py:884`, `interaction.py:398-409`), not new
aliases.

---

## 6. Validation and error model — two gates, on purpose

The split is forced by a fact about the runtime: **at config-load time
there is no deck.** The sidecar's hotplug loop runs happily in
`DEVICE_ABSENT` with nothing plugged in (`main.py` module docstring).
Anything requiring `key_count` therefore cannot be checked in
`load_config()`.

### Gate 1 — `config.load_config()`, capability-blind, **fail closed**

Raises `ConfigError` → clear stderr message → non-zero exit, matching
`AGENTS.md` § *Config* ("no default silently skips") and every existing
check in `load_config` (`config.py:180-216`).

| Condition | Message shape |
|---|---|
| `controls` is not an object | `Config field 'controls' must be a JSON object, got list` |
| address fails the grammar | `Config field 'controls' has invalid control address 'key.1.press'. Valid forms: key.N, dial.N.turn, dial.N.push` |
| value is not a string | `Config field 'controls' value for 'key.0' must be a string, got 3` |
| action not in catalog | `Config field 'controls' has unknown action 'connect' for 'key.0'. Valid actions: none, page_cycle, page_first, page_next, page_picker, page_prev, session, view_all, view_cycle, view_picker` |
| kind mismatch | `Config field 'controls': action 'view_picker' cannot be bound to 'dial.0.turn' — a dial turn accepts only: page_cycle, view_cycle` |

All five are decidable from the file alone. A typo never reaches the deck.

### Gate 2 — `layout.plan_layout(caps, controls)`, capability-aware, **report, never refuse**

Returns `LayoutPlan.unapplied` — one entry per binding that cannot apply to
*this* deck, each with the address and a concrete reason:

```
key.20        this deck has 15 keys (key.0 - key.14)
dial.2.push   this deck has no dials
dial.3.turn   this deck has 2 dials (dial.0 - dial.1)
```

**Why not fail closed here:** the deck is hot-pluggable. Refusing to start
when a binding doesn't match would make the sidecar unstartable whenever
the deck is unplugged — strictly worse than the condition being reported,
and it would break the `DEVICE_ABSENT` idle state that is core to this
repo (`AGENTS.md` § *Hotplug state machine is core, not polish*). The same
config becomes fully valid again the moment the right deck appears.

Where `unapplied` surfaces — four places, so it cannot be missed:

1. **Bring-up log**, once per device connect, next to
   `describe_plan` (`main.py:999`), at WARNING.
2. **`status.json`** via `reporter.update(...)` alongside `device_caps`
   (`main.py:1002`) — so `muxplex-deck status` shows it with no deck
   attached.
3. **`doctor`** — one new check.
4. **`muxplex-deck controls`** — the resolved table (§8).

### Gate 2 advisory warnings (never errors)

Legal but self-defeating configurations. Reported with `!`, never blocked —
a user who only ever uses the `all` view is entitled to unbind `view_picker`.

| Condition | Warning |
|---|---|
| no control resolves to `session` | `no control is bound to 'session' — this deck cannot connect any session` |
| `view_picker` bound, but no `page_prev`/`page_next`/`page_cycle` | `the view picker is bound but nothing pages it — views past the first page will be unreachable` |
| nothing bound to `view_picker`/`view_cycle`/`view_all` | `no control changes the view — the deck will stay on whatever view the server has` |

---

## 7. Picker mode is derived, not configured

While a picker is open, controls pre-empt their normal-mode meaning. That
already happens in the code (`main.py:765-774`, `interaction.py:398-409`);
this design makes the mapping explicit and keeps it out of config:

| Normal-mode binding | Meaning while a picker is open |
|---|---|
| `view_picker` / `page_picker` | BACK — close the picker (`ACTION_CANCEL`) |
| `page_prev` / `page_next` | page the option list (`ACTION_PAGE`) |
| `session` | option slot — tap selects (`ACTION_SELECT`) |
| `view_cycle` / `page_cycle` (turn) | scroll the picker window (`main.py:710`, `main.py:740`) |
| `none`, `view_all`, `page_first` | ignored (`ACTION_IGNORE`) |

This is a one-line change to `interaction.handle_picker_key`'s `kind`
comparisons (`interaction.py:398-409`) — the same three-way branch, keyed
on catalog names instead of `KEY_*` constants. It is why the picker needs
no second config surface, and why the "picker unreachable past page 1"
advisory in §6 is derivable rather than hand-maintained.

---

## 8. Discoverability

### 8.1 `config set controls` must be refused, not accepted

`cli.config_set` (`cli.py:209-234`) auto-detects value type from the
*default's* type. For a `{}` default, every `isinstance` branch misses and
control falls through to `else: value = raw_value` (`cli.py:227-228`) —
which would store the **string** `'{"key.0": "next"}'` where a dict
belongs, write it to disk, and print a success line. `load_raw_config`
would then hand a string to the planner.

That is precisely the silently-corrupted-configuration failure class this
repo hit five times on 2026-07-28. So a dedicated subcommand group is not
cosmetic — it exists because the generic one **cannot** be correct here.

Required, non-negotiable:

- `config set controls <anything>` → error, exit 1, pointing at
  `muxplex-deck controls set`.
- `config get controls` → pretty-printed JSON.
- `config list` → renders `controls: 3 bindings (modified)`, not a raw
  dict dump, so `config list` stays scannable.

### 8.2 The `controls` subcommand group

Reuses `report.py`'s VERDICT/STATE/ACTION renderer and its `!`/`+`/`-`
glyphs (`report.py:50-52`), same as `doctor`/`status`/`service`. Per the
gutter law (`report.py:33-36`), a healthy table renders with **no glyphs at
all** — the binding rows are `Readout` lines; only problems become `Check`
lines.

| Command | Does |
|---|---|
| `muxplex-deck controls` | Show the **resolved** table for the connected deck: address, action, and `(default)` / `(configured)`. Plus any `unapplied` entries and advisories. |
| `muxplex-deck controls actions` | Print the catalog — this is the user's *"list of available options to choose from"*: each action, its kind, and one line of what it does. |
| `muxplex-deck controls set <address> <action>` | Validate against the grammar + catalog + kind rules, then write one binding. |
| `muxplex-deck controls unset <address>` | Remove one binding → back to default. |
| `muxplex-deck controls reset` | Delete the whole `controls` key. |

Sketch of `muxplex-deck controls` with one bad binding (illustrative
layout, exact spacing owned by `report.py`):

```
Controls: 2 configured, 1 cannot apply to this deck

     key.0         session          (configured)
     key.4         view_picker      (configured)
     key.10        page_prev        (default)
     key.14        page_next        (default)
     key.1-3,5-9   session          (default)
     key.11-13     session          (default)

  !  dial.2.turn   this deck has no dials

Do this:
  Remove the binding, or plug in a deck with dials:
      muxplex-deck controls unset dial.2.turn
```

When no deck is attached, the table is computed from `status.json`'s
last-seen `device_caps` and labeled as such — never presented as current.

### 8.3 `doctor` and `status`

- `doctor` gains one check: `+ controls  3 bindings, all apply` or
  `! controls  1 binding does not apply to this deck`, with the
  `controls` command as its action line.
- `status` shows the unapplied count when non-zero.
- `init_wizard.py` is **not** touched. First-run should stay a
  three-question path; control mapping is a later, optional act.

### 8.4 `README.md`

One new section next to the existing "Config" reference (`README.md:383-438`
covers the current keys), with the catalog table, the address grammar, and
examples §4.3–§4.5.

---

## 9. Modules touched

| Module | Change | Rough size |
|---|---|---|
| `config.py` | `DEFAULT_CONFIG["controls"]`, `Config.controls` field, Gate-1 validation, address parser | ~90 |
| `layout.py` | `default_bindings(caps)`, `plan_layout(caps, controls)`, `LayoutPlan` reshape, derived properties, Gate-2 `unapplied`, `describe_plan` update | ~140 |
| `interaction.py` | `KEY_*` → catalog names in `handle_picker_key`; promote the two dead `press` methods | ~20 |
| `main.py` | `handle_key` dispatch by action name; `_make_dial_callback` dispatch by address; generalize `_paint_control_keys` + `_paint_reduced_picker` to iterate bindings with a per-action label spec; log + report `unapplied` | ~180 |
| `main.py` (new actions) | dispatch + logic for the 11 Tier-2 actions: `self.brightness` + `self.previous_session` fields, brightness clamp/floor, refresh thread, dead-session guard | ~80 |
| `rendering.py` | none — `render_control_key` (`rendering.py:304`) already takes `title`/`body`/`footer` | 0 |
| `cli.py` | `controls` group (5 subcommands), `config set controls` refusal, `config list` special-case, `doctor` check | ~220 |
| `cli.py` (catalog help) | `controls actions` text for 19 actions | ~40 |
| `statusfile.py` | carry `unapplied` | ~15 |
| `tests/` | §10 | ~430 |
| `README.md` | new section + expanded catalog table | ~110 |

**~1,325 lines against a 10.5k-line `src/`.** Up ~225 from the pre-expansion
estimate for **11 additional actions** — roughly 20 lines each, all-in.

The reason the marginal cost is that low is the point of the whole exercise:
the schema, both validation gates, the CLI group, the merge model, and the
paint generalization are **fixed costs already paid** by Tier 1. Every
action added afterward is a catalog row, a `match` arm over existing
machinery, a help line, and a test. §12 weighs whether the fixed cost was
worth paying; the marginal cost is not in question.

---

## 10. Test requirements

The load-bearing one first.

1. **Zero-config equivalence (the strongest guarantee in this design).**
   For every capability fixture in `tests/test_layout.py`
   (`CAPS_ORIGINAL_15:30`, `CAPS_PLUS:37`, `CAPS_XL:44`, `CAPS_MINI:51`,
   plus the degenerate grids at `test_layout.py:230-259`), assert that
   `plan_layout(caps, {})` produces,
   for **every** key index `0..key_count-1` and every dial address, the
   same `classify_key` result as the pre-change implementation. Encode the
   expected results as literal tables captured from v0.9.4 — not by calling
   the new code twice. A user who never configures anything must see zero
   change, and this is what proves it.
2. Gate-1 rejection: one test per row of the §6 table, asserting
   `ConfigError` and that the message names both the offending value and
   the valid set.
3. Gate-2 reporting: a Deck+ config loaded against Original caps yields
   exactly the expected `unapplied` entries — and a usable `LayoutPlan`,
   i.e. the run is **not** refused.
4. Merge: one-key override (§4.3) recomputes `session_slots` and
   `sessions_per_page` correctly; unmentioned controls keep defaults.
5. `none` vs absent: `"key.0": "none"` excludes key 0 from
   `session_slots`; absent leaves it at its default.
6. Picker derivation: with VIEW remapped to `key.4`, pressing `key.4`
   while the picker is open cancels (§7).
7. `config set controls` refuses with exit 1 and does not write.
8. Advisory warnings fire for each §6 condition and none is fatal.
9. Round-trip: `controls set` → `controls unset` → `controls reset` leaves
   the file byte-identical to the pre-`set` state.

### Added by the expanded catalog

10. **Kind-correctness across all 19 actions.** Table-driven: assert every
    action's declared kind, that every momentary action is accepted on
    `key.N` and `dial.N.push` and rejected on `dial.N.turn`, and the
    converse for the three relative ones. This is the test that keeps §2.0's
    split honest as the catalog grows.
11. **Brightness clamp and floor.** Repeated `brightness_down` stops at 10%,
    never 0 (§2.5); repeated `brightness_up` stops at 100;
    `brightness_cycle` with a large tick count clamps at both ends. Uses a
    fake device recording `set_brightness` calls.
12. **Brightness does not persist.** A reconnect (`_ActiveRuntime`
    reconstruction) reasserts `FULL_BRIGHTNESS_PERCENT`, and no brightness
    value is ever written to `config.json`.
13. **`toggle_last` dead-session guard.** Toggling to a session that has
    since disappeared from the server's list logs and no-ops — no failed
    connect, no error strip.
14. **`toggle_last` tracks server-side switches.** A session change observed
    via `_process` (someone switched in the PWA) updates the previous-session
    field, not just local key presses.
15. **`refresh_now` does not disturb the poll loop.** Asserts a manual
    refresh runs to completion concurrently with a poll cycle and that the
    loop's own timing/return contract is unchanged.
16. **`focus_app` advisory.** Binding `focus_app` with an empty `focus_app`
    config value produces the §6 advisory and is not fatal.
17. **New actions are inert in picker mode.** Every action outside §7's
    five-name set resolves to `ACTION_IGNORE` while a picker is open —
    table-driven, so a future action cannot silently acquire picker
    behavior.

Existing safety rails (`tests/conftest.py`, `AGENTS.md` § *Testing*) cover
config-path isolation already; no new rail is needed.

**Real-hardware sign-off is still mandatory** (`AGENTS.md`). The emulator
can drive `key.N` and `dial.N.turn|push` through `/input/key` and
`/input/dial`, which covers dispatch, and it implements `set_brightness`
(`emulator.py:174`) — but the emulator **cannot** prove brightness. Physical
sign-off must confirm: a remapped control key's paint, that
`brightness_down` visibly dims a real panel and stops at a readable floor,
and that `focus_app` on a bound key behaves as it does on connect.

---

## 11. Considered and rejected

### 11.1 Named profiles / per-model presets — REJECTED, violates the core invariant

`{"profiles": {"streamdeck-plus": {...}, "original-v2": {...}}}`. Requires
matching on `deck_type()`, which `AGENTS.md` forbids outright (Original and
MK2 collide; new models need matrix updates). Not a close call.

### 11.2 Bindings keyed by layout mode — REJECTED as speculative

`{"controls": {"full": {...}, "reduced": {...}}}`. Mode is capability-derived,
so this does *not* violate the invariant, and it would let one config file
serve two different decks on one machine. Rejected because:

- Config is per-machine (`~/.config/muxplex-deck/config.json`) and is not
  synced. The user's two decks live on two machines with two files. The
  problem it solves has not occurred.
- It doesn't fully solve even the hypothetical: an Original (15 keys) and a
  Mini (6 keys) are both REDUCED and would still collide on `key.14`.
- Gate 2 already handles deck-swap honestly — the user sees "3 bindings
  don't apply to this deck", not silence.
- Adding it later is additive (a new `controls_by_mode` key), so nothing is
  foreclosed.

Cost avoided: one nesting level in the schema, in the parser, in every CLI
subcommand, and in every error message.

### 11.3 A general event/binding engine — REJECTED, the largest over-engineering risk

Conditions, chained actions, macros, per-action parameter dicts, a plugin
registry for user-defined actions. This is a personal-scale tool with at
most 13 controls and 10 actions. A `dict[str, str]` and a `match` statement
cover the entire requirement. The engine version would be ~5× the code for
zero additional user-reachable behavior.

### 11.4 Touch-strip bindings in v1 — REJECTED for v1, needs its own design

The user's ask names the touch screen explicitly, so this is a real gap,
stated plainly rather than quietly dropped: **`_on_touch` has no dispatch
at all today** (`main.py:948-949`). The strip is an 800×100 status display.
Binding it is not a remap — it requires deciding what a tap *means*
spatially (a tap at x=300 on a headline that shows view + server + page:
does it hit the view field? does position matter at all?), plus SHORT vs
LONG vs DRAG semantics, plus a hit-test region model, plus its own
real-hardware sign-off. That is a separate design of comparable size to
this one. The address grammar extends additively when it is done.

Minimal version if wanted sooner: `touch.tap` = whole-strip short tap →
one momentary action, position ignored. ~60 lines. Listed in §13.

### 11.5 Parameterized actions (`connect:<name>`, pinned session keys) — REJECTED for v1

Genuinely useful (a key that always connects `agent-main` regardless of
page), but it changes the value type from `str` to `str | object`, and
raises questions this design has no answer for: what does the key paint
when that session doesn't exist? Does a pinned key participate in paging?
Does it count against `sessions_per_page`?

Deliberately **not** pre-built as an unused escape hatch — that is exactly
the speculative future-proofing `IMPLEMENTATION_PHILOSOPHY.md` warns
against. Since no config in the wild will contain a non-string value,
widening the value type later is trivially backward compatible. Say it in
the docs; don't build it.

### 11.6 `focus_app` as a standalone action — ~~REJECTED~~ **REVERSED, now SHIP**

**This entry originally said REJECTED. That was wrong once low-hanging
fruit became the explicit goal, and the reversal is recorded rather than
quietly edited away.**

Original reasoning: it would ship a *new capability* inside a feature whose
entire claim is "your current behavior is the default," so it belonged with
the open Windows focus decision instead.

Why that no longer holds: the objection was about **bundling**, not cost —
and it never disputed that the machinery works. `focus.focus_app(name)`
(`focus.py:52`) is complete, proven on macOS, and already invoked at
`main.py:849`. Exposing it is ~8 lines. It is also the only *available*
mitigation for the Windows service-context focus failure confirmed on
2026-07-28: auto-focus-on-connect can't win foreground from a Task
Scheduler process, but a user pressing a dedicated key at least makes the
attempt explicit and repeatable. Shipping it does not pre-decide the
`shell:startup` question — it is orthogonal.

Caveat carried into the catalog: if `focus_app` is unset, `focus.py:58-59`
returns immediately, so a bound key would do nothing at all. That is an
advisory warning (§6), not a silent no-op.

### 11.10 `sort_toggle` — DEFER, fails the observability constraint

Execution is genuinely cheap. `sort` has exactly two values
(`VALID_SORT_MODES = ("attention", "server")`, `config.py:31`) so it is a
toggle, not a cycle; `_process` re-derives order from `self.sort_mode` on
**every** poll (`main.py:423`), so flipping the field plus a
`refresh_now`-style re-poll would take effect immediately. ~15 lines.

Two problems, and the second is disqualifying:

1. **It is frequently invisible.** The only effect is tile *order*. With
   one session in the current view, or when both orderings coincide, the
   deck looks identical and the user cannot tell which mode is active — or
   that the press registered. Every other shipped action changes something
   the user can see (highlight moves, view name repaints, screen dims).
2. **It would create a new stale-state-reported-as-current bug.** `sort` is
   a persisted config key. A runtime toggle either writes back to
   `config.json` (a control press silently rewriting the config file — bad)
   or doesn't (then `muxplex-deck config get sort` reports `attention`
   while the deck is running `server` — a config surface reporting stale
   state as current, which is *precisely* the class that produced five
   separate incidents on 2026-07-28).

Neither is unsolvable — the honest fix is a runtime-vs-configured
distinction surfaced in `status`/`controls` — but that is a config-model
change, not a binding. Defer until it can be done without lying.

### 11.11 `display_toggle` (blank / wake the deck) — KILL for v1

The candidate that sounds cheapest and isn't.

`deck.reset()` and `_safe_close` (`main.py`) exist, so "blank it" looks
like one call. But `reset()` is the wrong primitive here: it clears key
images, so waking requires a full repaint, and the paint-diff cache
(`self.last_key_state`) would have to be invalidated via
`invalidate_paint_cache()`. The *right* primitive is `set_brightness(0)` —
instantly reversible, no repaint.

That is where it stops being cheap. **How does the user wake it?** If the
toggle is bound to `key.5`, they must press `key.5` on a screen that is
completely black. If the answer is "any key wakes it," then the waking
press must be swallowed rather than firing that key's action — a stateful
input filter, i.e. exactly the kind of hidden mode §7 was designed to avoid
having a second of. And a deck stuck at 0% is indistinguishable from a
crashed sidecar, in a project where "is it actually running?" has already
consumed a full day.

`brightness_down` with a 10% floor (§2.5) delivers most of the value —
dim it way down for a dark room — with none of the wake problem. Kill.

### 11.12 `view_hidden` — DEFER, thin end of the parameterization wedge

Three lines: `ViewCycler` already treats `hidden` as first-class
(`interaction.py:75-90`). But `view_all` and `view_hidden` differ only by a
string argument, and `view_all` exists as a distinct action solely because
`ViewCycler.press` (`interaction.py:162-171`) is a real, separate function.
Adding `view_hidden` makes the "these are really one parameterized action"
reading obvious, and the honest next step from there is `view_go:<name>` —
which is §11.5's deferred parameterization. The `hidden` view is also
low-traffic; the picker reaches it in two presses.

Take the whole parameterized-action step deliberately, or not at all. Not
one string constant at a time.

### 11.7 Fail-closed on capability mismatch — REJECTED, would break hotplug

See §6, Gate 2. Refusing to start would make the sidecar unstartable with
the deck unplugged.

### 11.8 Reusing `config set` for `controls` — REJECTED, it is actively unsafe

See §8.1. `cli.py:227-228` would store a JSON string where a dict belongs
and report success.

### 11.9 Storing resolved defaults in the config file — REJECTED

Writing the full computed table into `config.json` on first run would make
the file self-documenting, but it freezes every user on the geometry that
shipped that day, turns every planner improvement into a migration, and
makes "is this a default or a choice?" unanswerable. Defaults stay
computed; the file holds only deltas.

---

## 12. Honest read: does this earn its complexity?

**Revised upward after the catalog expansion: from ~60% to ~80%.**

The original read said the payoff was narrow — on a 15-key deck the only
real choice was *where three control keys sit*, for ~1,100 lines. That
judgment was correct for the catalog it was judging. The expanded catalog
changes the arithmetic, and the reason is worth stating precisely:
**~225 more lines bought 11 more actions.** The expensive parts — schema,
two gates, merge model, CLI group, paint generalization — are fixed costs
that Tier 1 already pays. Once paid, actions cost ~20 lines each.

That reframes the feature. It is no longer "make the three control keys
movable." It is "the deck gains eleven verbs it does not currently have,"
including several with no path to invocation today at all:

- **Views cannot be stepped on a REDUCED deck.** Not slowly — *not at all*.
  Only the picker (open, page, tap). `view_prev`/`view_next` fix that in 8
  lines by reusing `ViewCycler.turn` verbatim.
- **Dials 2 and 3 on the Deck+ are inert** (`main.py:942-943`), and every
  dial on a dial-having touchless deck is inert (`layout.py:202`).
  `brightness_cycle` gives them an obvious job, ported from ~6 lines already
  working in `deck_probe/events.py:126-130`.
- **Three functions currently have zero callers** — `ViewCycler.press`,
  `Pager.press`, and effectively `Pager.go_to`'s jump-to-end path. They
  become `view_all`, `page_first`, `page_last`.
- **`refresh_now` and `toggle_last` are genuinely new verbs** built from
  proven machinery (`main.py:727` and `main.py:802` respectively).

What still does not earn it:

- The **fixed cost is still the bulk of the work** (~1,100 of ~1,325). If
  the schema and gates are not built, none of the cheap actions exist. The
  expansion improves the ratio; it does not remove the entry fee.
- It still adds a new "why isn't the deck doing what I configured?" failure
  class to a codebase that shipped **five** stale-state bugs on 2026-07-28.
  This is why `sort_toggle` is deferred (§11.10) rather than shipped
  because it was cheap — cheapness is not the bar; observability is.
- Painting generalization (`main.py:684-688`, `main.py:608-610`) remains
  the riskiest edit, and it is unchanged by the expansion.

**Sequencing (confirmed, unchanged):** stale-state regression tests first
(~3-4h, already on the board, highest leverage on the whole board), then
Tier 1 + Tier 2 as one unit on top of that net.

Ship the eleven Tier-2 actions **with** Tier 1, not as a follow-up. They
share every fixed cost, and splitting them means paying the review,
release, and hardware-sign-off overhead twice for ~225 lines.

---

## Open questions

Short list; each needs a judgment call, not more research.

*Resolved since the first draft: **scope** (Tier 1 + 2, expanded catalog)
and **sequencing** (stale-state regression tests first). `focus_app` moved
from deferred to shipping — see §11.6.*

1. **The three non-ships.** Confirm or overturn: `sort_toggle` DEFER
   (§11.10, unobservable + would create a config-vs-runtime divergence),
   `display_toggle` KILL (§11.11, black-screen wake problem),
   `view_hidden` DEFER (§11.12, parameterization wedge). `sort_toggle` is
   the one I'd most expect you to push back on — it is cheap, and you may
   value it enough to accept a `status`-only readout.
2. **Brightness persistence.** §2.5 says brightness is session-local and
   resets to 100% on every reconnect, to avoid fighting `main.py:991`'s
   deliberate always-assert-full-brightness decision. If you want a dimmed
   deck to *stay* dimmed across replugs, that is a different decision and
   needs a config key plus a visible readout.
3. **Touch strip.** The ask named it; §11.4 still defers it. Accept, or
   take the minimal version (whole-strip tap → one momentary action,
   position ignored, ~60 lines)? Note §2.0: touch is also the most likely
   trigger for a third action kind.
4. **`LayoutPlan` reshape.** Unchanged from the first draft, and now
   carrying more weight since 11 more actions ride on it. This design
   replaces three stored fields with a bindings map and derived properties,
   touching six consumers (§1.2). Clean refactor of pure, well-tested code —
   but a refactor of working code. Accept, or bolt overrides onto the
   existing three-field shape (cheaper, but caps the feature at "move the
   three control keys" and cannot express `none`, dial bindings, or any of
   the Tier-2 catalog)?
5. **Cross-deck configs.** Confirm the assumption in §11.2: each machine
   has one deck and one config file, so mode-keyed bindings are not needed.
   If you ever expect to swap a Deck+ and an Original on the *same*
   machine, say so now — it is cheap to design in and awkward to retrofit
   into the CLI's error messages.
