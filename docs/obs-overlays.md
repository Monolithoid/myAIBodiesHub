# OBS overlays — placing them on the stream

Two pages are meant to be added to OBS as **browser sources**:

| Overlay | Service | URL | Default layout |
|---|---|---|---|
| NAO's voice and subtitle | `obs-agent` (`robotics/drones/obs/overlay.html`) | `https://<obs-agent domain>/overlay?token=$OBS_TOKEN` | subtitle centred at the bottom, box shrunk to the text |
| Games | `game-master` (`robotics/games/overlay.html`) | `https://<game-master domain>/overlay?token=$GAME_TOKEN` | whole screen, transparent, no frame: meant to sit over a background picture (e.g. a chalkboard) |

Both pages have a transparent background and fill the whole browser source. There are
two ways (they can be combined) to put them where you want.

## 1. Full-screen source, placed in OBS

Give the browser source the size of the OBS canvas (e.g. **1920 × 1080**, or
1280 × 720): the page then matches the stream pixel for pixel, and the default
layout is what the viewers see. To move or shrink an overlay in a scene, use OBS:

- **Transform** (right-click the source → Transform → Edit Transform) to move or
  scale it;
- **Crop** (Alt + drag a side, or the Crop fields of Edit Transform) to keep only the
  part with the panel, then move it.

Each scene may hold its own copy of the source with its own transform, so the layout
can change with the scene without touching the pages.

> Keep the source size equal to the canvas. A smaller source re-flows the page
> (the subtitle wraps sooner) instead of scaling it. The games panel keeps the same look
> at 1920 and 1280 px wide (its scale follows the width).

## 2. Custom CSS of the browser source

OBS adds the **Custom CSS** field of a browser source after the page's own CSS. Both
pages expose their layout as CSS variables on `:root`; overriding a variable is enough,
and these names are kept stable.

### Voice overlay (`#caption`, `#unlock`)

| Variable | Default | Role |
|---|---|---|
| `--caption-top` | `auto` | distance from the top (set it and `--caption-bottom: auto` to move the subtitle up) |
| `--caption-bottom` | `6%` | distance from the bottom |
| `--caption-left` / `--caption-right` | `4%` / `4%` | the box is centred between these two; set one to `auto` to stick to the other side |
| `--caption-max-width` | `92%` | widest box before the text wraps |
| `--caption-font-size` | `30px` | text size |
| `--caption-text-align` | `center` | text alignment inside the box |
| `--caption-bg` / `--caption-color` | `#000` / `#ffff00` | colours (classic yellow on black) |
| `--caption-radius` | `0` | corner rounding (square by default) |

Stable hooks: `#caption` (the subtitle, `.on` while shown), `#unlock` (red "Son
bloqué" banner, `.on` while shown).

### Games overlay (`#panel`)

| Variable | Default | Role |
|---|---|---|
| `--panel-top` / `--panel-right` / `--panel-bottom` / `--panel-left` | `0` (all four) | position (set the unused sides to `auto`) |
| `--panel-width` | `auto` | panel width (`auto` = between left and right) |
| `--panel-padding` | `4% 5%` | margin inside the panel |
| `--panel-scale` | `2` at 1920 px, `1.333` at 1280 px, `1` below | zooms the whole panel (text, gallows, ranking); the offsets above are zoomed too |
| `--panel-bg` | `transparent` | panel background |
| `--panel-border-width` / `--panel-border` / `--panel-radius` | `0` / light / `0` | frame (none by default) |
| `--panel-color` | `#f4f4f6` | text and drawings |
| `--panel-accent` | `#9fd3ff` | titles |
| `--panel-highlight` | `#ffe08a` | last event, ranking leader |

Stable hooks: `#panel` (`.shown` while visible), `#title`, `#view`, `#event`,
`#footer`, `#scores`, and one class per game view inside `#view` (`.hangman`, with
`.big`, `.def`, `.tried`, `.words`, `.errors`; `.investigation`, with `.intro`,
`.victim`, `.grid`, `.card`, `.clues`, `.who`). A new game view gets its own class and
uses these variables for its colours.

### Examples

Games panel back in a framed box in the bottom-right corner (without a background
picture):

```css
:root { --panel-top: auto; --panel-left: auto; --panel-right: 24px; --panel-bottom: 24px;
        --panel-width: 520px; --panel-scale: 1; --panel-padding: 16px 20px;
        --panel-bg: rgba(18, 18, 28, 0.86); --panel-border-width: 2px; --panel-radius: 14px; }
```

Full-screen games panel with a larger margin (to stay inside the chalkboard's frame)
and chalk-like colours:

```css
:root { --panel-padding: 8% 10%; --panel-accent: #f7f3c6; --panel-highlight: #ffd1dc; }
```

Games panel in the top-left corner only, half the screen wide:

```css
:root { --panel-bottom: auto; --panel-right: auto; --panel-width: 480px; }
```

Subtitle at the top of the screen, smaller text:

```css
:root { --caption-top: 5%; --caption-bottom: auto; --caption-font-size: 24px; }
```

Subtitle on the left, left-aligned, half the screen wide at most:

```css
:root { --caption-right: auto; --caption-max-width: 50%; --caption-text-align: left; }
```

Changes to the Custom CSS apply when the source is refreshed (OBS does it on
**OK**). Do not put the token or any secret in the Custom CSS.

## 3. OBS scene follows the activity

The program scene can switch by itself when an activity (a game) starts or ends, so
each scene places the overlays its own way (section 1). The code decides, not the LLM;
only the scene changes (no sources, no audio).

```
game-master ── GET /state (every ACTIVITY_POLL_S) ── body-gateway ── /activity ──► static drone ── obs-websocket ──► OBS
```

- **body-gateway** publishes `/activity` (`{"activity": "hangman" | "story" | null}`, see [stories.md](stories.md)) on every
  change and again every ~15 s. `ACTIVITY_POLL_S` (default 2, `0` = off); nothing is
  published while game-master cannot be reached.
- **The static drone** (same local network as OBS) switches the scene through
  obs-websocket v5 (`robotics/drones/static/onboard/obs_scenes.py`). In its `.env`:

  | Variable | Meaning |
  |---|---|
  | `OBS_WS_URL` | `ws://<ip of the OBS PC>:4455`; empty = feature off |
  | `OBS_WS_PASSWORD` | password of OBS's WebSocket server (Tools > WebSocket Server Settings) |
  | `OBS_SCENES` | `activity=scene` pairs, e.g. `default=Plateau,hangman=Pendu,mystery=Enquete,story=Conteur`; `default` is the scene with no activity, and for an activity without its own scene |

A saved mystery stays the running activity between two streams, so `/activity` keeps
saying `mystery` until the case is solved or cancelled.

Only **changes** switch the scene: the first value the drone receives after it starts
is just remembered (a restart during a stream does not force a scene), and between two
changes the streamer keeps control of OBS. OBS closed, wrong password or unknown scene
→ a warning in the drone's log, nothing else.
