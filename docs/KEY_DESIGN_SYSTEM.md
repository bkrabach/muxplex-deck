# Key Face Design System

How a muxplex-deck key face is composed. Rules, not per-key tweaks.

This document is **design only** — no implementation. Where it contradicts what
`rendering.py` / `main.py` do today, the code is what ships and this is the target.

---

## 0. The medium, and why normal UI instinct is wrong here

| Property | Consequence for design |
|---|---|
| Each key is its own square LCD, separated by bezel | No shared canvas. No gutters, no cross-key alignment except what the eye infers. Every face is a self-contained composition. |
| 72px (Original/MK2/Mini/XL), 96px (Neo), 120px (Plus) | Everything must be a formula on face edge `S`, never a per-model table. |
| Physically ~18mm at 72px, viewed at 50–75cm | ~0.70px per arcminute at 600mm. This is the binding constraint — see §2. |
| No hover, no focus, no cursor, no scroll | Nothing can be revealed progressively. Everything a face will ever say is on it now. |
| Rendered by PIL: fill, TTF text, rectangle stroke, crop | No gradients worth the CPU, no rounded corners, no shadows, no icon set. |
| `ImageFont.load_default()` is **Aileron Regular — one weight** | **There is no bold.** Hierarchy must be carried by size and value, not weight. |

> The last row corrects a premise in the original critique. "VIEW in lighter-weight
> fonts" describes `#8888aa` ink, not a lighter font — the product has exactly one
> weight and always has. Any weight-based hierarchy would require vendoring a TTF.

---

## 1. Zone model

**Every key face — session tile, control key, picker option — is the same three
horizontal bands inside a uniform margin. The bands are reserved whether or not
they hold ink.**

```
┌──────────────────────┐  ← border stroke, B px, at the face edge
│ ┌──────────────────┐ │
│ │      NAME        │ │  what this key IS
│ ├──────────────────┤ │
│ │                  │ │
│ │      BODY        │ │  the discriminator — what tells this key from its neighbours
│ │                  │ │
│ ├──────────────────┤ │
│ │      STATE       │ │  live ambient context you did not cause
│ └──────────────────┘ │
└──────────────────────┘
```

**The three questions, in order:**

1. **NAME** — *what does this key control?* Fixed for the life of the key.
2. **BODY** — *the single most discriminating string on this face.* If you covered
   everything else, this is what still tells you which key you are looking at.
3. **STATE** — *where am I?* Changes without you pressing this key.

Everything horizontally centred. Everything reserved even when empty, so a key
with no STATE puts its BODY at exactly the same y as a key that has one.

### Geometry

All derived from face edge `S`. No model names anywhere.

| Token | Formula | S=72 | S=96 | S=120 |
|---|---|---|---|---|
| `B` border stroke | `max(2, round(S/36))` | 2 | 3 | 3 |
| `M` content margin | `round(S/18)` | 4 | 5 | 7 |
| `NAME_H` | `round(0.28·S)` | 20 | 27 | 34 |
| `STATE_H` | `round(0.19·S)` | 14 | 18 | 23 |
| `BODY_H` | `S − 2M − NAME_H − STATE_H` | 30 | 41 | 49 |
| content width | `S − 2M` | 64 | 86 | 106 |

Band origins at S=72: NAME `y 4..23`, BODY `y 24..53`, STATE `y 54..67`.
Sums are exact at every size — no rounding drift.

`NAME_H` is sized to hold one PRIMARY line (`ascent 16 + 4` at S=72), because on a
session tile the NAME *is* the primary read. `STATE_H` holds one SECONDARY line.

### Vertical centring rule (prevents per-key jitter)

Centre the **ink bbox of a fixed reference string (`"Hxg"`)**, not the bbox of the
actual text. Otherwise a name with a descender sits 2px higher than one without,
and the deck looks drunk. This is a real defect in the current code, which centres
each string's own bbox.

### BODY may be a *field* instead of a *string*

A session tile's terminal preview is not a string in the BODY band — it is a field
that **underlays the whole content box, bottom-anchored**, with the NAME band
composited on top of it. This is what the current session tile already does and
why the user judges it the best-looking face.

Consequence worth stating: computing preview line count against the **full content
height** rather than `height − banner` yields **4 lines at S=72 and 8 lines at
S=120** — identical to today's hardware-verified Deck+ count. The margin fix costs
zero preview lines.

### How the reported inconsistency falls out

Every complaint is one violation of "NAME is always present, BODY always holds the
discriminator, zones are always reserved":

| Today | Violation |
|---|---|
| `page_prev` renders `"" / "< PREV" / "p1/2"` | **No NAME.** Indistinguishable from `view_prev`, which renders `"VIEW" / "< PREV"`. This is exactly the user's complaint. |
| `page_picker` renders `"PAGE" / "PAGE"` | BODY repeats NAME — carries zero discriminating information. |
| `toggle_last` renders `"" / "TOGGLE" / ""` | No NAME, no STATE — two of three bands empty and unreserved. |
| `view_picker` STATE = hostname | Hostname is install-constant, not ambient state. Wrong band, and it is the only key that shows it. |
| Control BODY centred on the **whole face**, ignoring bands | Works by accident today; breaks the moment band heights differ. |

---

## 2. Type scale

**Three sizes. One weight. Two ink values. That is the entire scale.**

| Role | Size | Ink | S=72 | S=96 | S=120 |
|---|---|---|---|---|---|
| **PRIMARY** — the one string you actually read | `round(2S/9)` | `#FFFFFF` | 16 | 21 | 27 |
| **SECONDARY** — known closed vocabulary, recognised not read | `round(11S/72)` | `#8888AA` | 11 | 15 | 18 |
| **TEXTURE** — never read; shape recognition only | **fixed 11** | `#7A7A7A` | 11 | 11 | 11 |

**Exactly one PRIMARY string per face.** That is the load-bearing rule. Two
PRIMARYs on one face means you have not decided what the key is for.

### Why these numbers, and why not more of them

At 600mm, 1 arcminute ≈ 0.70px on a 72px face. Measured Aileron cap heights:

| Size | Cap height | Angular | Verdict |
|---|---|---|---|
| 11 | 8px | ~11 arcmin | Enough to **recognise** a word you already know (`PAGE`, `PREV`). Not enough to **read** an unfamiliar one. |
| 15 | 10px | ~14 arcmin | Threshold for scanning unfamiliar text. |
| 16 | 11px | ~16 arcmin | Comfortable for a session name you are hunting for. |
| 11 @ 5.5px advance (preview) | 8px, ~4px wide glyphs | ~6 arcmin | **Below letter-identification threshold.** This is texture, and honestly labelling it so is the point. |

Everything else in the current code is an unjustified intermediate. The scale
collapses six distinct sizes to three:

- `16` session name, `20` picker label, `15` control body → **PRIMARY**
- `11` control title, `11` control footer, `11` status key → **SECONDARY**
- `11` preview → **TEXTURE** (kept separate because it must not scale — see below)

Picker labels drop 20 → 16. Justification: at 20 only ~6.4 lowercase characters fit
the content width, so long view names ellipsise; at 16, ~7.9 fit. And a picker
option and a session tile are the *same act* — choose a thing from a grid — so they
should be the same size.

**TEXTURE does not scale with `S`, deliberately.** Its value is *column count*, not
apparent size; scaling it up on a 120px face would reduce columns and regress the
hardware-verified 21-column Deck+ preview.

Because PRIMARY scales with `S`, the **character budget is face-size-independent:
~7 characters at every deck size.** Truncation rules need no per-deck tuning.

---

## 3. The state-vs-content collision

### The actual defect

`render_session_key` draws the border at inset 0 with width 4 (or two 3px rings =
6px when active *and* needing attention), while `_fit_label` permits text to reach
`width − 4`, i.e. `x = 2`. **The text is allowed into the border's own pixels** —
2px of overlap per side normally, 4px in the dual-ring case. The collision is
horizontal, not vertical, and it is worst exactly when both states apply.

### Resolution

**`M = B + gap`, and `M` is set so content clears the border for any `B ≤ M − 1`.**
At S=72: `B = 2`, `M = 4`, leaving a 2px clear gap. Text is fit to `S − 2M`, never
to `S − 4`.

The design survives `B = 3` with a 1px gap, so the border thickness can be settled
on hardware without redesigning anything.

### But stacked rings are the wrong answer, and thinner rings do not fix them

The user proposed thinner borders plus more padding. That is right for the *single*
state and I have adopted it. It does **not** fix the dual state: two rings of any
useful thickness eat 4–6px of a 4px margin, and in the mockup they read as "a muddy
target symbol" rather than two statuses. Concentric rings are a bad encoding — they
put two signals on the same channel at the same place and force both to be thin.

**Use two orthogonal channels instead:**

| State | Channel | Cost in content pixels |
|---|---|---|
| **Active session** | Cyan ring, `B` px, at the face edge | 0 (lives in the margin) |
| **Needs attention** | NAME band fill turns amber `#F1A640`, band ink inverts to `#000000` | 0 (the band already exists) |

Both can be present simultaneously without touching each other — the band is inset
by `M`, so the ring never crosses it. Neither is thin. Neither can be eaten by the
other. And the inversion means attention is signalled by **two** redundant changes
(fill and ink polarity), not just hue.

Measured contrast: `#000000` on `#F1A640` ≈ **10.4:1**. `#FFFFFF` on `#F1A640` is
2.0:1 — that is why the ink inverts rather than staying white.

Intended salience order: **attention outranks active.** A filled amber band is far
more salient than a 2px ring, and that is correct — attention is a summons, active
is a fact.

---

## 4. Colour model

Two independent channels. They never collide because they never appear in the same
place.

**Background = what kind of key this is** (a category, never a state):

| Fill | Means |
|---|---|
| `#0A0A0A` near-black | a session lives here |
| `#101036` indigo | a control or a chooser |
| `#000000` black | nothing here |

**Foreground = state** (never a fill of the whole face):

| Mark | Means |
|---|---|
| Cyan `#00D9F5` ring | this is the live one — active session, or current option in a picker |
| Amber `#F1A640` NAME band + inverted ink | this wants you |

Both hues are lifted verbatim from muxplex's `frontend/style.css`, so the deck and
the PWA speak the same language. Keep it that way.

### Is colour load-bearing?

Proportionate answer for a personal tool, but the answer is genuinely clean:
**no state in this system is signalled by hue alone.**

- *Active* is signalled by **ring present vs absent** — a shape channel. A user who
  cannot see cyan still sees a ring.
- *Attention* is signalled by **band fill + ink polarity inversion** — a value
  channel. Survives total colour loss.
- The two are never distinguished *from each other* by hue, because they occupy
  different geometry.

The one thing hue does carry alone is background category (session vs control), and
that is also carried by content (a terminal preview is unmistakably not a label).

### One correction to current values

`#A8A8A8` preview ink on `#0A0A0A` is **8.6:1** — the preview currently
out-contrasts the `#8888AA` control captions (4.9:1) and competes with the session
name it sits beneath. The least important thing on the face is the highest-contrast
thing on the face. Drop preview ink to `#7A7A7A` (≈4.4:1).

---

## 5. Density under scale

**A 72px face holds one PRIMARY string of ~7 characters plus one non-textual
signal. Everything beyond that costs contrast and buys nothing.**

Empirically confirmed in the mockup: `"deckwork"` — 8 characters — clips the 64px
content width at PRIMARY size. `MAX_SESSION_LABEL_CHARS = 10` is dead weight; it
was tuned for 120px keys and is never the binding limit at 72px. Measured fitting
(`_fit_label`) should be the only gate.

**Does this system make the 15-key deck better or worse?**

| | Change | Net |
|---|---|---|
| Session name | Gains a guaranteed 2px clear gap from the border; loses ~1 character of width | **Better** — it was being clipped by the border, which is worse than losing a character |
| Preview | 4 lines at 72px, unchanged (see §1, field-underlay); ink dimmed | **Neutral-to-better** — dimming stops it competing with the name |
| Control keys | Gain a NAME band that disambiguates them | **Better** — this is the reported bug |
| Picker options | 20 → 16, ~1.5 more characters before ellipsis | **Better** |

**Better on the thing you read, neutral on the thing you glance at.** That is the
right trade, because the name is the decision and the preview is the reassurance.

**The honest ceiling.** The preview at 72px renders ~4px-wide glyphs — roughly
6 arcminutes, below the threshold at which a letter can be identified. It is not
text and will never become text at this size. It is kept because "recognise my
session by its shape" is a real and useful signal, and because the user likes it.
It should be labelled TEXTURE in the code and dimmed accordingly, so nobody later
tries to "improve its legibility" — there is no legibility there to improve.

---

## 6. Worked specification per key type

### 6.1 Session tile

| Band | Content | Role |
|---|---|---|
| NAME | session name, measured-truncated | **PRIMARY**, `#FFFFFF` on `rgba(0,0,0,195)` band |
| BODY + STATE | terminal preview, field-underlaid, bottom-anchored, full content box | TEXTURE |
| border | cyan ring if active | — |
| NAME band fill | amber + black ink if needs attention | — |

The only face where NAME is the PRIMARY. Justified: the key *is* that session; the
name is what you hunt for; the preview is the live content. Nothing changes about
this face except margins, ring thickness, attention encoding, and preview ink.

### 6.2 Control key

Layout is invariant. Content comes from the table below; the rule generating it is
**BODY holds the discriminator, NAME holds the qualifier, STATE holds live ambient
context.**

| Action | NAME (SEC) | BODY (PRIMARY) | STATE (SEC) |
|---|---|---|---|
| `view_picker` | `VIEW` | *current view name* | `n/N` view position |
| `view_prev` | `< PREV` | `VIEW` | `n/N` |
| `view_next` | `NEXT >` | `VIEW` | `n/N` |
| `view_all` | `GO TO` | `ALL` | — |
| `page_picker` | `PAGE` | `n/N` | — |
| `page_prev` | `< PREV` | `PAGE` | `n/N` |
| `page_next` | `NEXT >` | `PAGE` | `n/N` |
| `page_first` | `FIRST` | `PAGE` | `n/N` |
| `page_last` | `LAST` | `PAGE` | `n/N` |
| `brightness_up` | `BRIGHT` | `+` | `nn%` |
| `brightness_down` | `BRIGHT` | `-` | `nn%` |
| `toggle_last` | `SWAP TO` | `LAST` | *previous session name* |
| `refresh_now` | `DECK` | `SYNC` | — |
| `focus_app` | `HOST` | `FOCUS` | — |
| `none` | — | — | — (blank face) |

**The critical inversion.** `view_prev` and `page_prev` today both shout
`"< PREV"` in PRIMARY, and only a dim `"VIEW"`/nothing distinguishes them — the
exact adjacency the user complained about. In the mockup, a vision review still
could not tell `VIEW/< PREV` from `PAGE/< PREV` apart quickly. So the discriminator
and the qualifier swap places: **the noun goes big, the direction goes small.**
`< PREV` over `VIEW` and `< PREV` over `PAGE` are then trivially separable, and the
direction is still legible *and* redundantly encoded by chevron side and by physical
key adjacency.

Chevrons stay ASCII (`<` `>`). The default PIL font has no arrow glyphs and renders
`.notdef` boxes — proven on hardware with U+2192.

Hostname is dropped from the view key. It is install-constant, it appears on exactly
one key, and it is already on the touch strip where a strip exists.

### 6.3 Picker option

| Band | Content |
|---|---|
| NAME | — (reserved, empty; all 12 options would say the same word) |
| BODY | option label — view name or page number. **PRIMARY** |
| STATE | — (reserved, empty) |
| border | cyan ring if this is the current view/page |

Deliberate exception to "NAME always occupied": in picker mode *every* key is the
same category, so a repeated category label is pure noise. Uniform within the type,
which is what the anti-raggedness rule actually requires.

### 6.4 Picker chrome (reduced layout: BACK / PREV / NEXT)

Ordinary control keys. Same table discipline:

| Key | NAME | BODY | STATE |
|---|---|---|---|
| back | `< BACK` | `VIEW` or `PAGE` (whichever picker is open) | — |
| prev | `< PREV` | `PAGE` | `n/N` |
| next | `NEXT >` | `PAGE` | `n/N` |

### 6.5 Status key (strip-less decks)

The one face that breaks the zone model, correctly: an error message is
free-form text of unknown length and there is nothing else on the face to align to.
Word-wrapped SECONDARY on `#000000`, filling the content box. Honest signal over
composition; the log carries the detail.

---

## 7. What I rejected, and why

| Rejected | Why |
|---|---|
| **Font-weight hierarchy** | Impossible. `ImageFont.load_default()` is Aileron Regular, single weight. Vendoring a TTF costs licensing, packaging, and binary size for a benefit that size + value already delivers. |
| **Concentric dual rings** (status quo) | Two signals on one channel in one place, forcing both thin. Reads as a muddy target. Replaced with two orthogonal channels (§3). |
| **Thinner rings alone** (the user's proposal, taken literally) | Fixes the single-state overlap — adopted — but leaves the dual-state case still eating the margin. Half the fix. |
| **Arrow/icon glyphs** | Default PIL font renders `.notdef` boxes. Proven on hardware. |
| **An icon set / vendored symbol font** | A packaging dependency to replace six ASCII words that already work. |
| **Rounded corners, gradients, drop shadows** | Cost pixels and CPU at 18mm to communicate nothing. |
| **A design-token module / theming layer** | Nobody will ever vary these. Six named constants plus the formulas in §1 and §2, in `rendering.py`, *is* the system. Ruthless simplicity. |
| **Per-model geometry tables** | Violates the repo's central architectural rule. Everything is `f(S)`. |
| **Multi-line wrapped session names** | Halves the cap height or eats the preview. A measured 7-character truncation is more honest than unreadable two-line text. |
| **Keeping hostname on the view key** | Install-constant, not state. Wrong band, one key only, and duplicated on the strip. |
| **A 5th/6th/7th type size** | Every intermediate size is unjustifiable against the arcminute table in §2. Three sizes is what the medium actually supports. |
| **Any progressive disclosure** | There is no hover, focus, or scroll. Everything a face will say is on it now. |
| **Dark-amber band with amber ink** for attention | Evaluated in the mockup as the low-bloom alternative. Reads as muddy and under-salient — attention should shout. Retained only as the fallback if bright amber blooms on hardware (§8). |

---

## 8. What cannot be settled without hardware

These are judgements about an emissive LCD at arm's length. A 6× nearest-neighbour
upscale on a monitor is a poor proxy, and a vision model reading that upscale is
worse. Each of these has a named fallback so nothing blocks on it.

| # | Question | Fallback if it fails |
|---|---|---|
| 1 | **Is a `B=2` cyan ring unmistakable in peripheral vision?** Highest-risk item. Vision review flagged it as too thin twice; I discount that (it was judging a grey-backed upscale, and line-detection acuity on a saturated emissive edge is far better than letter acuity), but I cannot settle it from here. | `B=3`. `M=4` already guarantees a clear gap at `B=3`, so nothing else changes. |
| 2 | **Does bright amber `#F1A640` bloom and smear the black NAME-band text?** Small dark text on a saturated bright fill can smear on these panels. | Darken the band fill (`#C97F1E`) keeping black ink; or fall back to dark-amber band with amber ink. |
| 3 | **SECONDARY at cap-height 8px (~11 arcmin) — is `PAGE` recognisable at a glance, or does it need size 12?** | Bump SECONDARY to `round(12S/72)`. STATE_H already has the headroom. |
| 4 | **Does the continuous NAME band actually read as continuous across the bezel gaps?** The whole "one horizontal band across the deck" claim rests on this. | If not, the zone model still holds per-key; only the cross-key claim is lost. |
| 5 | **Session tile NAME is PRIMARY at top; control key BODY is PRIMARY at middle. Adjacent, does that look ragged?** | Accept — the two key classes are already distinguished by background field, and no alternative keeps both the preview and a readable name. |
| 6 | **Optical mass with an empty STATE band.** Geometry is identical, but a key with nothing in the bottom band looks bottom-light next to one that has it. Flagged twice in review. | The §6.2 table already populates STATE almost everywhere; `view_all`, `refresh_now`, `focus_app` are the only blanks. Live with it, or drop those three to a 2-band variant — which would reintroduce exactly the raggedness this document exists to remove, so: live with it. |
| 7 | **Preview at `#7A7A7A` — still recognisable as "my session's shape"?** | Split the difference at `#909090`. |
| 8 | **Picker at PRIMARY 16 instead of 20** across a full 12-key grid. | Revert to 20 for pickers only — but that reintroduces a fourth size, so prefer accepting shorter labels. |

**Cheapest way to settle 1–3 and 7:** render candidate faces through the existing
emulator (`muxplex-deck --emulator`, `/keys/N.jpg`) and look at them on the real
panel via a one-off paint, before committing any of this to `rendering.py`.
Per `AGENTS.md`, the emulator is for iteration, not sign-off — 1, 2 and 4 in
particular are exactly the class of thing emulation has already been caught missing.
