# Soft Deck — a phone/tablet as a muxplex deck

**Status:** DESIGN ONLY. No code written. Awaiting the decisions in
["Open questions"](#open-questions).
**Ask (verbatim):** *"what about creating native Android (and later iOS) apps
that can serve as muxplex-deck interfaces if I wanted to prop one up next to my
laptop on the go?"*
**Ground truth as read:** muxplex v0.19.0 (live on `spark-1`), muxplex-deck
v0.12.0, both `AGENTS.md`, `docs/CONTROL_MAPPING_DESIGN.md`,
`docs/KEY_DESIGN_SYSTEM.md`, `muxplex/muxplex/{main,auth,tls,settings}.py`,
`muxplex/muxplex/frontend/{index.html,manifest.json,app.js,style.css}`.

---

## 0. The finding that reframes the question

**The soft deck already exists and already works. You have been using it as a
terminal.**

Open the muxplex PWA on a phone today, tap a session tile, and `app.js`'s
`openSession()` (`frontend/app.js:3216`) does exactly two things:

1. `POST /api/sessions/{name}/connect` — which repoints the single ttyd and
   sets the server-global `active_session`. **Your laptop's PWA follows it**
   (muxplex #15; the sibling fix for `active_view` shipped in v0.7.1).
2. Mounts xterm.js over the `/terminal/ws` relay and navigates the phone into
   the terminal view.

Step 1 *is* the deck. Step 2 is everything that makes it the wrong tool for the
job: it navigates you off the grid, so switching again costs a Back tap; it
opens a WebSocket relay and a terminal emulator you will never look at; and it
drags ~196 KB of `app.js` plus the xterm vendor bundle onto a device that
wanted to render twelve rectangles.

So the real question is not *"should I write a native app?"* It is:

> **The tile-tap verb is wrong. Where does the right verb live?**

That is a much smaller question, and it does not have a native answer.

Two other findings, verified against this machine, do most of the rest of the
work:

- **The cert problem is already solved in the server and simply not switched
  on.** `muxplex setup-tls --method tailscale` exists (`cli.py:1566` lists
  `tailscale` among the `--method` choices; `tls.py:509` implements it).
  Tailscale is installed on `spark-1`, and `tailscale status --self --json`
  reports `CertDomains: ["spark-1.tail8f3c4e.ts.net"]` — HTTPS certs are
  **already enabled on your tailnet**. One command yields a publicly-trusted
  Let's Encrypt certificate. This removes the single strongest argument for
  native (§3.1).
- **The PWA is already installable, with no service worker.** Per MDN,
  Chromium's installability criteria are `name`/`short_name`, 192px + 512px
  icons, `start_url`, `display`, `prefer_related_applications` absent, and
  HTTPS. `frontend/manifest.json` satisfies every one. A service worker is
  *not* required. iOS 16.4+ installs from the Share menu in Safari, Chrome,
  Edge, Firefox and Orion.

---

## 1. Problem framing

**Goal.** A phone or small tablet, propped beside the laptop, that shows the
current view's sessions and switches the laptop between them with one tap —
glanceable, no unlock-and-hunt, no keyboard.

**What it is not.** It is not a terminal, not a second full client, and not a
replacement for the PWA. If you want to *read* a session on the phone, the PWA
already does that well. The soft deck is a **control surface**, exactly like
the physical deck: it changes what the laptop is showing, and it tells you what
is shouting.

**Scope boundary.** The muxplex server's `/api/*` contract is the integration
seam and must not grow to accommodate this. Everything a soft deck needs
already exists:

| Need | Endpoint | Notes |
|---|---|---|
| what's in the current view, sorted, with bells | `GET /api/view` | server-resolved; explicitly built to be "cheap for frequent polling (e.g. a Stream Deck dial)" |
| pane previews | `GET /api/sessions` | `/api/view` deliberately omits snapshots |
| switch | `POST /api/sessions/{name}/connect` | |
| change view | `PATCH /api/state` (`active_view`) | server-global, last-writer-wins |
| detect settings/view-membership edits | `settings_updated_at` on `GET /api/state` | avoids a second poll |

Zero new server endpoints. Two optional, additive follow-ups are named in §9.

---

## 2. Explicit assumptions

Stated so they can be shot down rather than silently relied on.

1. **The phone reaches `spark-1` over Tailscale**, not over the open internet.
   "On the go" means both devices are on the tailnet. No public exposure is
   contemplated anywhere in this document.
2. **One human, one tool.** No multi-user story, no app store distribution, no
   support burden beyond your own.
3. **The phone is propped, not held.** It sits on the desk beside the laptop.
   It is glanced at and reached over — thumb-zone ergonomics matter less than
   target size and legibility at ~30–45 cm.
4. **Sessions are on the order of 5–30, not 500.** The whole current view fits
   on one or two phone screens.
5. **You will not pay an ongoing toolchain tax.** Every component in this
   project so far shipped in a day. A design whose *maintenance* exceeds that
   is disqualified regardless of its features.
6. **You do not need offline.** A deck with no server is a picture of a deck.
7. **iOS is a real requirement, later, on the same hardware budget** — i.e.
   "later" means "same codebase, no new machine, no new subscription," not
   "a second project."

Assumptions 5 and 7 do most of the killing in §7.

---

## 3. Does "native" buy anything a well-built PWA does not?

This is the load-bearing question and it deserves item-by-item honesty. Two of
these are real native wins. The rest are already available on the web and were
simply never switched on.

### 3.1 Certificate pinning — *the strongest native argument, and it dissolves*

**The real problem is not pinning. It is trust.** Today `spark-1` presents a
leaf signed by `CN = muxplex Local CA` (verified: `openssl x509 -in
~/.config/muxplex/muxplex.crt -noout -issuer`). A phone browser has never heard
of that CA, so it shows a full-page interstitial. Worse than annoying: an
origin with a certificate error is not a valid secure origin, so **Chrome will
not offer to install it as a PWA** — which takes wake lock, standalone display
and the home-screen icon down with it.
*(That the install prompt specifically is suppressed by a bypassed-interstitial
origin is my expectation from Chrome's installability rules, not something I
verified on device. It does not change the recommendation, because the fix
below removes the interstitial entirely.)*

A native app fixes this by pinning or by bundling the CA. That is a genuine
capability the web platform does not have (HPKP was removed years ago and has
no successor).

**But you do not have this problem.** `muxplex setup-tls --method tailscale`
already exists and your tailnet already has cert domains enabled. It produces a
real Let's Encrypt certificate for `spark-1.tail8f3c4e.ts.net`. Every phone on
earth trusts it out of the box. No CA install, no interstitial, no lost PWA
features — and, as a side effect, `muxplex-deck`'s `ca_file` config key becomes
unnecessary on both your machines.

So: pinning is real, and irrelevant. Pinning is *hardening* against a
compromised public CA. The onboarding problem it was going to solve is solved
better, one layer down, by a command you already own. Building an Android app
to avoid running one command on the server is not a trade anyone should take.

The honest costs of that switch are in §10 — they are not zero.

### 3.2 Screen always on — **already available to the web**

The Screen Wake Lock API became Baseline in May 2024: Chrome 84+, Firefox 126+,
**Safari 16.4+ on macOS and iOS**. `navigator.wakeLock.request("screen")`,
re-acquired on `visibilitychange`, is exactly what a propped-up deck wants and
it works on both target platforms. Not a native win.

### 3.3 Haptics — **a real native win on iOS only, and it is small**

`navigator.vibrate()` works on Android Chrome. **iOS Safari does not implement
the Vibration API at all** and Apple has shown no intent to. A native iOS app
gets `UIImpactFeedbackGenerator`; a PWA gets nothing (the `<input
type="checkbox" switch>` trick that circulates is a side effect, not an API,
and I would not build on it).

Weigh it honestly: this is a tap-confirmation nicety on one of two platforms,
on a device sitting on a desk in front of you where the confirmation is *also*
visual (the tile you tapped highlights instantly). It is not worth a second
toolchain.

### 3.4 Install friction — **near-parity, with an iOS asterisk**

- Android: Chrome shows an install prompt automatically; `beforeinstallprompt`
  lets the page trigger it from its own button. Result is a WebAPK — real
  launcher icon, real task-switcher entry, no browser chrome.
- iOS 16.4+: install is Share → Add to Home Screen. Manual, and
  `beforeinstallprompt` does not exist, so the app cannot prompt. One-time
  friction, once, ever.
- Native, sideloaded: *worse* on iOS. Free Apple provisioning expires in **7
  days**; a year requires a $99/yr Developer account. Android sideloading needs
  "install unknown apps" and manual re-install per update.

Native does not win install friction. On iOS it loses it.

### 3.5 App-switching persistence — **parity in practice, worth testing**

An installed PWA on Android is a WebAPK with its own task-switcher entry and is
backgrounded/restored like any app. iOS aggressively evicts backgrounded web
views, so a returning deck may cold-start and re-fetch. For a surface whose
entire state is one `GET /api/view` away, a cold start costs ~200 ms and looks
identical. **Where it would bite is auth** — see §8's `session_ttl` note.

### 3.6 Background behaviour — **irrelevant by design**

A deck has nothing to do when you are not looking at it. Push notifications for
bells are conceivable but explicitly out of scope: the muxplex bell already
reaches you through the laptop, and adding Web Push would mean a VAPID key
pair, a subscription store, and a server-side sender — real complexity for a
device that is sitting 30 cm from your face with its screen on.

### 3.7 Offline — **meaningless here**

### 3.8 Verdict

| Capability | Native-only? | Verdict |
|---|---|---|
| Cert pinning | Yes | Solved better by `setup-tls --method tailscale`; pinning was never the need |
| Screen always on | **No** — Baseline since 2024 | Web wins |
| Haptics | iOS only | Real, tiny, not worth a toolchain |
| Standalone/no browser chrome | **No** — `display: standalone` | Parity |
| Install friction | No — native is *worse* on iOS | Web wins |
| App-switch persistence | Marginal | Parity in practice |
| Background/push | Yes, if ever wanted | Out of scope |
| Offline | Yes | Irrelevant |

**Native buys one small thing (iOS haptics) at the cost of the single largest
recurring maintenance liability in this entire project.** That is not close.

---

## 4. The medium shift — what carries over from the key design system, and what does not

v0.12.0 shipped `docs/KEY_DESIGN_SYSTEM.md` for a 72×72 px emissive square
viewed at 50–75 cm, with no hover, no focus, no scroll, and one font weight.
**A phone is a different medium in almost every dimension that document
reasons about.** The system's *rules* survive; its *numbers* do not.

### 4.1 Carries over — the rules

| Rule | Why it still holds |
|---|---|
| **Zone model** (NAME / BODY / STATE, bands reserved whether or not they hold ink) | The anti-raggedness argument is medium-independent, and it is *more* valuable with more tiles on screen. Becomes: name row / preview / state row, with the state row reserved. |
| **Exactly one PRIMARY per face** | Still the discipline that forces you to decide what a tile is for. |
| **Two orthogonal state channels** — active = ring in the margin, attention = filled band with inverted ink | Ports verbatim. Costs zero content pixels in CSS too. |
| **No state signalled by hue alone** | Free to keep, so keep it. |
| **Palette** `#00D9F5` cyan, `#F1A640` amber, `#0D1117` bg | Already shared: `KEY_DESIGN_SYSTEM.md` §4 lifted them *from* `frontend/style.css`. Round-tripping them keeps deck, PWA and soft deck speaking one language. |
| **"BODY may be a field"** — preview underlays the content box, name composited on top | This is why the session tile is the best-looking face on the physical deck. Same trick, same reason. |
| **Discriminator goes big** | The v0.12.0 inversion (`VIEW`/`PAGE` large, `< PREV` small) is a general truth about scannable labels. |

### 4.2 Does **not** carry over

| Thing | Why it dies |
|---|---|
| **The type scale** (16 / 11 / 11 px at S=72) | Derived from an arcminute budget at 600 mm on an 18 mm square. A phone at ~350 mm with DPR 2–3 has roughly an order of magnitude more angular resolution to spend. Every number is wrong here. |
| **The ~7-character name budget** | Gone. Full session names fit comfortably. `MAX_SESSION_LABEL_CHARS` has no analogue. |
| **TEXTURE as a category** | At 72 px the preview renders ~4 px glyphs — *below letter-identification threshold*, which §5 of the key design system honestly labels texture. **On a phone the preview is genuinely readable.** It graduates from texture to content, and that changes what a tile is *for*: on the deck the preview is shape-recognition; on the phone it can actually tell you what the agent is doing. This is the single biggest capability gain of the medium. |
| **`f(S)` geometry formulas** | CSS has `clamp()`, `min()`, container queries and `dvh`. Do not port PIL arithmetic into a stylesheet. |
| **"Nothing can be revealed progressively"** | The hardest constraint on the physical deck is simply absent. Scroll, long-press, sheets and swipe all exist. |
| **Fixed key count and paging** | Scrolling replaces paging outright. `page_prev`, `page_next`, `page_first`, `page_last`, `page_cycle`, `page_picker` — six of the nineteen catalog actions — are *meaningless* here. |
| **The `key.N` binding model** | There is no `key.7` on a phone. See §4.4. |
| **`brightness_*`** | Belongs to the OS. Three more actions gone. |

### 4.3 Is a grid of tiles even the right metaphor?

Partly. The sharpest thing the medium tells us is this:

> **A Stream Deck's real value is spatial constancy — you learn that
> `amplifier-main` is top-left and stop reading the key.** `sort: "attention"`
> reorders the deck, which is tolerable on hardware you look at directly and
> **hostile to muscle memory** on a surface you want to hit without looking.

A phone has surface area the deck does not, so it can have both, and should:

- **A stable-order grid** as the primary surface. Large tiles, order that does
  not move. Attention is a *decoration on the tile* (amber band), never a
  re-sort. **The soft deck's default sort should be `server`, inverting the
  sidecar's `attention` default.** This is a real, medium-derived
  recommendation, not a preference.
- **An attention strip** — a thin row at the top listing only sessions that
  currently need attention. This is what `sort: attention` was *actually* for,
  and on the deck it had to be expressed as re-ordering because there was
  nowhere else to put it. Here there is.

A pure scrolling list is the other credible metaphor and is better if session
count grows past ~24 or names grow long. I recommend the grid because spatial
constancy is the thing worth protecting; the list is the fallback if the grid
proves cramped in your hand. Flagged in §12 as an open question, because it is
a judgement about your session count, not a fact I can derive.

### 4.4 Does the binding model extend?

**No, and it should not be forced to.** `key.N` / `dial.N.turn` addresses exist
because the hardware has a fixed, small, indexable control set — that is the
entire justification in `CONTROL_MAPPING_DESIGN.md` §0. A phone has an
unbounded, reflowing surface. "Assign an action to slot 7" is not a question
the medium poses.

What the 19-action catalog reduces to on a phone:

| Physical-deck actions | Fate on a soft deck |
|---|---|
| `session` | **Is the whole surface.** Every tile. |
| `view_picker`, `view_prev`, `view_next`, `view_all`, `view_cycle` | Collapse into **one native affordance**: a view name in the header that opens a sheet. Prev/next are what a segmented control or a horizontal swipe is for. |
| `page_*` (6 actions) | **Deleted.** Scroll. |
| `brightness_*` (3) | **Deleted.** OS. |
| `toggle_last` | Keep — a single "⇄ *previous-session-name*" chip. Genuinely useful, and legible here in a way it is not at 72 px. |
| `refresh_now` | Keep as pull-to-refresh. Costs nothing, is the platform idiom. |
| `focus_app` | **Does not apply** — see §6.3, this is a real functional gap. |
| `none` | Meaningless. |

**Config model:** the soft deck has essentially nothing to configure. Sort
order and grid density are two `localStorage` values behind a settings sheet.
Do not build a binding table. Do not build a config file. If a preference ever
needs to be shared with the laptop, it belongs in `PATCH /api/settings`, not in
a second config system.

---

## 5. The candidates

Five, of which three are serious.

### A — Deck **mode** inside the existing overview grid

A `?deck=1` / toggle state in `app.js` that (i) changes the tile-tap verb from
`openSession()` to connect-and-stay, and (ii) restyles the grid. No new files,
no new route.

### B — A new **route** on the same origin: `/deck/`  ← *recommended*

A new directory `muxplex/frontend/deck/` containing `index.html`, `deck.css`,
`deck.js` and its own `manifest.json` (`start_url: "/deck/"`). Consumes
`/api/view` + `/api/sessions`, posts `/connect`, patches `active_view`.

**Costs zero Python.** `main.py:2748` already mounts
`_NoCacheStaticFiles(directory=_FRONTEND_DIR, html=True)` at `/` — with
`html=True`, `frontend/deck/index.html` is served at `/deck/` with no route
handler. It also inherits the load-bearing `Cache-Control: no-cache` behaviour
the mount exists to provide, and ships inside the existing wheel and release
process.

### C — A **separate web app**, own origin/host

Same code as B, deployed somewhere else (a second port, a static host).

### D — **True native**, one app per platform (Kotlin/Compose + Swift/SwiftUI)

### E — **Cross-platform native** (Flutter / React Native / .NET MAUI)

*(Two "reuse what exists" options are considered and rejected in §11: pointing
the phone at the muxplex-deck **emulator's** web UI, and running the sidecar
itself on the phone.)*

---

## 6. Tradeoff analysis

Fixed 8-dimension frame. Qualitative ratings with a concrete note; numeric
scores would be false precision.

| Dimension | **A — mode in app.js** | **B — /deck/ route** | **C — separate web app** | **D — native ×2** | **E — cross-platform native** |
|---|---|---|---|---|---|
| **Latency** | Good — but pays 196 KB `app.js` + xterm vendor on every cold start | **Best** — a few KB, no terminal, no WebSocket; single `GET /api/view` to first paint | Best (same code) | Best | Good — larger cold start than a 20 KB page |
| **Complexity** | Deceptively bad — `app.js` is 5,120 lines and `test_frontend_js.py` carries **229 regex assertions against its source text**, a documented refactor tripwire (`muxplex/AGENTS.md`) | **Best** — one HTML + one CSS + one JS, no build step, no npm, no framework, isolated from the tripwire | Same code, plus deployment | Two languages, two UI frameworks, two build systems | One language, but a whole SDK, plus Xcode/Gradle underneath |
| **Reliability** | Regressions in the deck can break the terminal client — shared blast radius | **Best** — a broken deck cannot break the PWA; independent files, independent failure | Good | Good | Good, until an SDK major bump |
| **Cost (build)** | ~150–300 lines, but into the riskiest file in the repo | **~500–700 lines, greenfield** | Same + hosting | Two full UI builds; iOS needs a Mac | One UI build + two release pipelines |
| **Cost (maintain)** | Every future `app.js` refactor must not break deck mode | **Lowest** — no dependencies to age; the API contract is versioned and additive by policy | Low, plus a second deploy target | **Highest** — 2 toolchains, 2 signing stories, $99/yr, annual OS-target churn | High — SDK upgrades break things annually; still needs a Mac + Apple account for iOS |
| **Security** | Same-origin, httpOnly cookie, no new secret | **Same** — inherits auth middleware and TLS unchanged; **no secret ever lands on the phone** | **Worse** — cross-origin ⇒ CORS, a second auth path, and a real temptation to ship the federation Bearer key to the device | Tempting to store the federation key in Keystore/Keychain — **that key also unlocks `POST /api/sessions/{name}/input`, i.e. RCE.** Explicitly do not want this on a phone | Same concern as D |
| **Scalability** | Fine | Fine — bounded by session count, not by design | Fine | Fine | Fine |
| **Reversibility** | Poor-ish — entangled in a large file | **Best** — `rm -rf frontend/deck/` and it never existed | Good | **Worst** — store listings, signing identities, installed users | Worst |
| **Org fit (one person, ships in a day)** | Adequate | **Excellent** | Adequate | **Disqualifying** | **Disqualifying** |
| **Optimizes for** | Least new code | **Least new *concepts*; zero server change; iOS free** | Deployment independence | Platform-native polish | One codebase across two native platforms |
| **Sacrifices** | Isolation, page weight, blast radius | A second manifest and ~100 lines of duplicated fetch/render helpers | Same-origin auth simplicity | Everything above | Everything above, slightly cheaper |

**The dominant tradeoff is `Cost (maintain)` × `Org fit`.** Every option
delivers the function; they differ by an order of magnitude in what they cost
you *forever*. Latency and complexity are the tiebreak between A and B, and
`test_frontend_js.py` is what settles it.

---

## 7. Recommendation

> **Build `muxplex/frontend/deck/` — a second, tiny, same-origin page inside
> the existing muxplex frontend, installed to the phone's home screen as its
> own PWA — and switch the server to a Tailscale-issued Let's Encrypt
> certificate first.**

Reasoning, in order of weight:

1. **iOS is free.** The same URL, the same page, Share → Add to Home Screen.
   No Mac, no Xcode, no $99/yr, no second codebase. Assumption 7 alone
   eliminates D and E.
2. **The auth and transport problems are already solved and simply not turned
   on.** Same origin ⇒ the existing `httpOnly` `muxplex_session` cookie
   authenticates the deck with zero new code, and `setup-tls --method
   tailscale` removes the certificate wall entirely.
3. **The server does not change.** `/api/view` was explicitly designed for a
   frequently-polling deck client. This satisfies the "add nothing to the
   server" constraint literally.
4. **It is deletable.** If it is wrong, `rm -rf frontend/deck/`.
5. **It stays out of `app.js`.** A 5,120-line file guarded by 229
   source-text assertions is the wrong place to prototype a new surface, and
   that suite has already produced two false failures on legitimate refactors
   (v0.13.0, v0.16.1).

**What I am giving up, stated plainly:** ~100 lines of fetch/poll/render
helpers will be written twice (once in `app.js`, once in `deck.js`). That is
the price of isolation and I think it is obviously worth paying at this size —
but it is a real duplication and the API-semantics-drift risk that
`muxplex/AGENTS.md` warns about applies. Mitigation: `deck.js` must consume
`/api/view` and never re-derive the bell predicate, view membership, or sort
order locally. The whole reason `/api/view` exists is so a second client can be
written without porting logic.

### 7.1 Sketch of the recommended surface

```
┌──────────────────────────────────────────┐
│  ▾ agents                     ⇄ scratch  │   header: view name (tap → sheet)
├──────────────────────────────────────────┤     + toggle-last chip
│  ▲ amplifier-main   ▲ deckwork           │   attention strip (only if non-empty)
├──────────────────────────────────────────┤
│ ┌────────────────┐  ┌────────────────┐   │
│ │ amplifier-main │  │ deckwork       │   │   NAME  — PRIMARY, full name
│ │ ───────────────│  │ ───────────────│   │
│ │ $ pytest -q    │  │ Waiting for... │   │   PREVIEW — now readable content,
│ │ 906 passed     │  │                │   │   not texture
│ │           2m   │  │          14m   │   │   STATE — reserved band
│ └────────────────┘  └────────────────┘   │
│ ┌────────────────┐  ┌────────────────┐   │   cyan ring = active
│ │ muxplex        │  │ scratch        │   │   amber name band = attention
│ ...                                       │
└──────────────────────────────────────────┘
```

Behaviour, complete:

- Poll `/api/view?sort=…` + `/api/sessions` every ~2 s while visible; **stop
  polling on `visibilitychange`** (a backgrounded deck must not drain battery
  or hammer the server).
- Tap tile → optimistic highlight, then `POST /connect` on the wire —
  mirroring the sidecar's "optimistic repaint, never block" rule
  (`muxplex-deck/AGENTS.md`). The next poll reconciles.
- Tap view name → bottom sheet of `views` from `/api/view` → `PATCH
  /api/state`.
- Pull to refresh = `refresh_now`.
- Wake lock acquired while visible, re-acquired on `visibilitychange`.
- `navigator.vibrate(10)` on tap, feature-detected, silently absent on iOS.
- Long-press a tile → *nothing in v1.* (Candidates for v2 in §12.)

---

## 8. Auth and TLS — the actual first-run walkthrough

### 8.1 What happens today, unchanged, if you just open it on the phone

1. `https://spark-1:8088` — MagicDNS resolves the short name on the tailnet.
   The current leaf's SANs are `spark-1, spark-1.local, localhost,
   spark-1.tail8f3c4e.ts.net, 127.0.0.1, ::1, 192.168.1.5` (verified), so the
   **hostname is fine**.
2. **Full-page certificate interstitial**, because the issuer is `CN = muxplex
   Local CA`. Tap-through works for browsing; it is expected to suppress the
   PWA install prompt and is a wall on every cold start.
3. To make it go away without changing the server, you would install
   `~/.config/muxplex/ca/muxplex-ca.crt` on the phone:
   - **Android:** Settings → Security → Encryption & credentials → Install a
     certificate → *CA certificate* → a deliberately alarming warning screen →
     requires a device lock PIN. Chrome then shows a persistent "your network
     may be monitored" notice.
   - **iOS:** download the profile, Settings → Profile Downloaded → Install,
     then a **second, separate step** most people miss: Settings → General →
     About → Certificate Trust Settings → toggle full trust on.
   - And you must not grab the wrong file — pointing at `muxplex.crt` (the
     leaf) instead of `ca/muxplex-ca.crt` is the exact mistake that cost real
     debugging time in muxplex-deck onboarding, and is why `GET /api/ca` and
     `muxplex-deck doctor`'s `basicConstraints` check exist.

**That is a bad first run, and it is the one everyone assumes is unavoidable.
It is not.**

### 8.2 The recommended first run

**Server, once:**

```
muxplex setup-tls --method tailscale     # → Let's Encrypt cert for
                                          #   spark-1.tail8f3c4e.ts.net
sudo systemctl --user restart muxplex    # (or however you restart it)
```

**Phone, once:**

1. Open `https://spark-1.tail8f3c4e.ts.net:8088/deck/` — **no interstitial**,
   green padlock, valid public certificate.
2. Redirected to `/login` (auth middleware, `auth.py:252`). PAM mode: your
   Linux username + password. → `httpOnly`, `SameSite=Strict` cookie.
3. Android: Chrome offers "Install app" (or the page's own button via
   `beforeinstallprompt`). iOS 16.4+: Share → Add to Home Screen.
4. Launches standalone, no browser chrome, own icon, own task-switcher entry.

**Steps 2–4 happen once, ever.** Step 1 is a bookmark.

### 8.3 The credential decision, and one thing to explicitly not do

**Use the session cookie. Do not put the federation Bearer key on the phone.**

That key is the same credential that satisfies `POST
/api/sessions/{name}/input` — remote code execution by design, per
`muxplex/AGENTS.md`. It should live on trusted hosts, not on a device that
travels. The cookie is strictly better here: it is `httpOnly` (unreachable from
script, unlike anything a web app could store), server-revocable via
`/auth/logout`, TTL-bounded, and **already built**. This is also the concrete
security cost of options C/D/E: a cross-origin or native client has no
same-origin cookie to inherit, which makes shipping the Bearer key the path of
least resistance.

### 8.4 The wrinkle a naive walkthrough misses: login always lands on `/`

`POST /login` ends with `RedirectResponse("/", status_code=303)`
(`main.py:2237`), unconditionally. `login.html`'s form posts to a bare
`/login` with no `next` parameter. So the real sequence on a cold, unauthenticated
launch of an installed deck with `start_url: "/deck/"` is:

```
tap deck icon → GET /deck/ → 307 /login → password → 303 "/"  ← the TERMINAL app
```

You end up in the wrong app and have to relaunch the deck. It happens exactly
once per cookie lifetime, which is precisely why §8.5 matters — at a 7-day TTL
this is a weekly papercut; at 90 days it is an annoyance you will forget about.

Three ways out, in increasing cost:

1. **Accept it.** Log in, tap the icon again. Zero code.
2. **Client-side:** `deck.js` cannot see the 307 (the browser follows it), but
   the deck page *can* detect on load that it was served the login document and
   show a "return to deck" link. Ugly.
3. **Server-side `?next=`:** ~5 lines in `post_login` plus a hidden field in
   `login.html`. Must be validated as a same-origin *path* (leading `/`, no
   `//`, no scheme) or it is an open redirect. Small, additive, and it is the
   only clean fix. This is the one place where I would accept a server change,
   and it is worth noting that it improves the existing PWA too (deep links
   currently lose their destination on login the same way).

### 8.5 The other piece of friction

`settings.session_ttl` defaults to **604800 s = 7 days** (`settings.py:37`),
and the cookie's `max_age` is set from it (`main.py:2243`). A phone deck would
therefore demand a Unix-password login **weekly**, which for a glanceable
control surface is exactly the wrong ratio of ceremony to value.

Recommendation: raise `session_ttl` for this deployment (e.g. 90 days). The
honest trade: a lost, unlocked phone becomes a live session switcher — and,
because the same cookie authenticates the whole PWA, a live *terminal*. Given
the surface is tailnet-only and the phone has a lock screen, I judge that
proportionate. It is a settings value, not an architectural change, and it is
yours to set.

---

## 9. What the server needs

**Nothing.** Two optional additive follow-ups, neither required for v1:

1. **`GET /api/view?snapshots=1`** — `/api/view` deliberately omits pane
   snapshots to stay cheap. The soft deck wants both the resolved view *and*
   previews, so v1 makes two requests per poll (exactly what the PWA already
   does). If that ever matters, an additive query param is ~10 lines and
   breaks no client.
2. **`session_ttl`** — a settings value, above.

---

## 10. Migration and rollout

Ordered. Each step is independently reversible.

| # | Step | Risk | Rollback |
|---|---|---|---|
| 1 | `muxplex setup-tls --method tailscale`, restart | **Breaks both muxplex-deck sidecars** — see below | Re-run `setup-tls --method ca` |
| 2 | Remove `ca_file` from `~/.config/muxplex-deck/config.json` on **MBP and ALIENWARE-R13** | Sidecar down until done | Restore the key |
| 3 | Open `https://spark-1.tail8f3c4e.ts.net:8088/` on the phone, verify padlock + login | None | — |
| 4 | Build `frontend/deck/`, verify at `/deck/` in a desktop browser | None — new files only | Delete the directory |
| 5 | Install to home screen, use it for a week | None | Uninstall |
| 6 | Consider raising `session_ttl` | Security, §8.5 | Lower it |

**Step 1 is the one with teeth, and it is easy to get wrong.**
`muxplex-deck`'s config sets `ca_file` to the local CA, and `httpx` with an
explicit CA bundle trusts **only** that CA — so the moment the server presents
a Let's Encrypt leaf, both sidecars fail verification. Steps 1 and 2 must be
done together, on both machines. After step 2 the sidecars use the system trust
store (certifi), which trusts Let's Encrypt, and the `ca_file` gotcha
documented in `SCRATCH.md` and `muxplex-deck/AGENTS.md` stops applying at all.

Also note: everything moves to the `.ts.net` hostname. A Let's Encrypt cert
covers `spark-1.tail8f3c4e.ts.net` and *only* that name — `https://spark-1:8088`
will start failing verification even though it works today.

**Recurring cost, stated because it is easy to hide:** Let's Encrypt
certificates are ~90-day. `muxplex` has **no renewal automation** — the only
renewal path is a human running `setup-tls` after `doctor`/startup warns
(`cli.py:695`, `cli.py:1382`). Switching to Tailscale turns a *yearly* chore
(the local-CA leaf currently expires 2027-06-07) into a *quarterly* one, and
the failure mode is a browser wall on the phone. A systemd timer running
`muxplex setup-tls --method tailscale` monthly plus a service reload is the
obvious fix and is a genuine follow-up task, not an afterthought. **If you are
not willing to own that, the local-CA path plus a one-time phone CA install is
the honest alternative — say so and I will re-plan around it.**

---

## 11. What I rejected, and why

| Rejected | Why |
|---|---|
| **Native Android app (Kotlin), then iOS (Swift)** | Buys iOS haptics and cert pinning. Pinning is moot once the cert is publicly trusted (§3.1); haptics is a tap confirmation on a device already showing you a visual confirmation. Costs two languages, two UI frameworks, a Mac, $99/yr, and re-implementing auth without the same-origin cookie. Directly violates assumptions 5 and 7. |
| **Flutter / React Native / MAUI** | The "one codebase, two platforms" pitch is real, but the *floor* is a whole SDK plus Xcode plus Gradle plus two signing identities — and iOS still needs a Mac and an Apple account. A cross-platform SDK is a permanent tax on a tool whose defining property is that its components ship in a day. |
| **Deck mode inside `app.js` (candidate A)** | Fewest lines, worst place to put them. `app.js` is 5,120 lines and `test_frontend_js.py` holds 229 regex assertions against its *source text* — a tripwire that has already failed two legitimate refactors. It also drags xterm and the full app shell onto a phone that needs neither, and couples deck regressions to terminal regressions. Kept as the fallback if the duplicated helpers in `deck.js` turn out to be more than ~100 lines. |
| **Separately-hosted web app (candidate C)** | Loses same-origin. That means CORS config, a second auth path, and the strong temptation to ship the federation key to the phone — trading away the single best property of the recommendation for deployment independence nobody needs. |
| **Point the phone at the muxplex-deck emulator's web UI** | Superficially the cheapest "reuse what exists": the emulator already serves `/state`, `/keys/N.jpg`, `/input/key` (`muxplex-deck/AGENTS.md`). But it renders **72 px JPEGs designed for an 18 mm LCD** — deliberately illegible at phone scale per `KEY_DESIGN_SYSTEM.md` §5 — and requires the sidecar to be running on some machine, exposing an unauthenticated HTTP surface. It reproduces the physical deck's constraints without any of its reasons. |
| **Run the muxplex-deck sidecar on the phone** | Python on Android via Termux is possible and grim; on iOS it is impossible. The sidecar's entire purpose is HID access, which is the one thing a phone does not need. |
| **Porting the `key.N` / `dial.N.turn` binding model** | §4.4. Capability-space addressing exists because the hardware has a fixed, indexable control set. A reflowing scroll surface does not pose the question. Building a binding table for a phone would be cargo-culting the physical deck's *constraints* as if they were its *design*. |
| **Porting the physical type scale and the 7-character budget** | Derived from a specific arcminute budget on an 18 mm square at 600 mm. Wrong by roughly an order of magnitude at phone distance and DPR. |
| **`sort: attention` as the soft deck default** | It reorders tiles, which destroys the spatial constancy that makes a deck a deck. The phone has room to express attention as a decoration *plus* a dedicated strip, so it should. §4.3. |
| **Web Push for bell notifications** | VAPID keys, a subscription store, a server-side sender, and a new muxplex endpoint — against the "add nothing to the server" constraint, for a device sitting 30 cm away with its screen on. |
| **A service worker / offline mode** | Not required for installability (verified against MDN). A deck with no server has nothing to show. Adds a cache-invalidation failure mode to a project where `Cache-Control: no-cache` on `app.js` is documented as load-bearing precisely because installed PWAs cache too aggressively. |
| **Cert pinning as a design goal** | It hardens against a compromised public CA. It was never the answer to "the phone shows a scary warning" — that is a *trust anchor* problem, solved one layer down. |
| **A design-token / theming layer for the soft deck** | Same reasoning `KEY_DESIGN_SYSTEM.md` §7 already applied: six CSS custom properties in one stylesheet *is* the system. |

---

## 12. Known gaps and honest uncertainty

1. **The phone cannot raise the laptop's window.** This is the one real
   functional regression versus the physical deck. `focus_app` works because
   the sidecar runs *on* the laptop (`main.py:849`); a phone has no such reach.
   Tapping a tile switches `active_session` and the laptop's PWA follows —
   but if the PWA is behind your editor, it stays behind your editor. A
   headless "focus-follower" mode in muxplex-deck (poll `active_session`, call
   `focus_app` on changes it did not cause) would close it, but note the
   sidecar currently sits in `DEVICE_ABSENT` with *zero server traffic* when no
   deck is plugged in, so this is a real behavioural change, not a flag. Not
   designed here.
2. **Two clients, one ttyd.** The phone and the laptop attach to the same ttyd
   for the same active session. I have not verified how a second attached
   client behaves in practice. The recommended design sidesteps this entirely
   by never mounting a terminal — but it is worth knowing before anyone
   proposes adding one.
3. **Install-prompt suppression on a bad cert** is my expectation from Chrome's
   installability rules, not a device-verified fact. It does not change the
   recommendation.
4. **iOS PWA storage/eviction behaviour** for a home-screen app is
   version-dependent and I have not verified it on your hardware. The design is
   resilient (all state is one fetch away), but re-login frequency on iOS is
   the thing most likely to be worse than predicted.
5. **Everything in §4 about legibility is reasoned, not measured.** The
   physical key design system was mocked and reviewed before shipping; nothing
   equivalent has been done here.

---

## Open questions

Each needs your judgement, not more research.

1. **The Tailscale cert switch.** It is the keystone of the whole first-run
   story (§8.2) but it turns a yearly cert chore into a quarterly one with no
   automation today, and it requires touching both sidecar configs in lockstep
   (§10). Accept, accept-with-a-renewal-timer, or reject and keep the local CA
   plus a one-time phone CA install?
2. **Grid or list?** §4.3 recommends a stable-order grid with a separate
   attention strip, on the argument that spatial constancy is what makes a deck
   a deck. A scrolling list carries more information per row and scales past
   ~24 sessions. How many sessions are typically in the view you would prop
   this next to?
3. **`session_ttl`.** Weekly Unix-password re-login on a glanceable control
   surface is the wrong ratio (§8.5). Raise it, and to what — or accept the
   weekly login?
4. **Separate app icon, or one?** The recommendation gives `/deck/` its own
   manifest, so the phone shows *two* muxplex icons (terminal and deck). The
   alternative is one icon plus a manifest `shortcuts` entry. Two icons is
   clearer for "prop it next to the laptop"; one is tidier. Your home screen.
5. **The focus gap (§12.1).** Live with it, or is "tap on phone → laptop window
   comes forward" important enough to justify a headless follower mode in
   muxplex-deck?
6. **What the deck may *do*.** v1 is switch + change view, nothing else. The
   API also offers create (`POST /api/sessions`), delete, and clear-bell. Long-
   press could reach them. Ruthless simplicity says no in v1 — confirm, or name
   the one you actually want.

---

## 13. Success criterion — what "it worked" means

*Added after a product-council review recorded a FAIL on this axis: the rollout
checkpoint ("use it for a week") named no criterion, so it could not distinguish
a week of real use from a week of never opening it, and the rollback
("uninstall") had no trigger.*

**Keep it if:** within the first two weeks, you reach for the phone deck
unprompted **3 or more times** in a situation where the physical deck was not
with you — without first falling back to alt-tab or the terminal PWA.

**Uninstall it if:** at two weeks you have to remind yourself it exists, or every
recalled use was you testing it rather than using it.

**The honest baseline nobody named during design:** alt-tab. The laptop is right
there, it costs nothing, and it has no auth story, no screen to keep awake, and
no CA cert to install. The deck has to beat *that*, not beat "no way to switch
sessions." Six independent review lenses converged on this being the single
largest unexamined competitor to the feature.

### What the council found, recorded rather than resolved

Five lenses returned CONCERN, one returned FAIL, and they explicitly declined to
converge — the disagreement is about calibration, not fact, and is left standing:

- The trigger frequency for "on the go" was never estimated anywhere across
  ~2,200 lines of design, despite the same corpus deriving pixel-level tile
  floors and citing WebKit bug numbers.
- "Ship nothing, tolerate the existing flow" was never among the candidate
  architectures, despite §0 stating outright that the switch function already
  works today at zero additional cost.
- Design effort exceeded the project's own stated appetite (every other
  component here shipped in a day) before a line of code existed.

**What actually happened:** the build ran in parallel with the review and landed
in the same session — ~700 lines, proven end-to-end against a real browser and a
scratch server. The bet resolved smaller than the review feared. That does not
retire the findings above; it means the cheap-to-reverse half was spent before
the question was asked. The expensive half has not been spent, and is gated:

### Hard gate on the one irreversible step

**Do not begin the TLS/Tailscale certificate migration** until either the
alt-tab baseline is explicitly ruled out, or the criterion at the top of this
section has been met. That migration breaks both working hardware sidecars
simultaneously (each pins `ca_file`) and converts a yearly certificate chore
into a quarterly one with no renewal automation. It is the only step in this
project that is not cheaply undoable.

Until then the deck runs over the existing self-signed CA, which costs a
one-time manual CA install on the phone and breaks nothing.

### Also flagged, not acted on

`session_ttl` is currently long, and the phone is the device most likely to be
lost. A found, unlocked phone carries a cookie that can drive terminals. Worth
shortening for this surface specifically — not changed here, because it affects
existing authenticated sessions on other devices.
