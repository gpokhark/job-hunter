# Forest themes + "Offer Season" manifestation layer — design

Date: 2026-10-03 · Branch: `feature/forest-offer-theme` (from `dev`) · Status: awaiting review

## 1. Goal

Give the radar an opt-in visual identity that makes the hunt feel hopeful and personal:

- Two selectable themes, **`forest`** (night) and **`forest-dawn`** (sunrise).
- A light **manifestation layer** (titles, affirmations, an "offer letter" card, a celebration
  moment) that frames the page positively without ever altering data.
- Personalisation with the applicant's **first name**, taken from the profile they already keep.

Success = a user sets `radar.theme: forest-dawn`, runs the pipeline, and gets the prototype look
(`brag-output/radar-forest-prototype/`) on the radar, the live radar and the Applications page;
a user who sets nothing gets **byte-identical output to today**.

### Non-goals (YAGNI)

`forest-auto` (time-based switching), a progress-driven sunrise, custom affirmation lists,
theming the profile-diff / refilter reports, any change to scoring, filtering or stored data.

## 2. Decisions already made

| Topic | Decision |
|---|---|
| Where the theme is chosen | `config/settings.yaml` → `radar.theme`, with `--theme` on `render_radar.py` as a per-run override |
| Default | `auto` = today's light/dark behaviour, nothing injected |
| Titles (Strong matches section, page, vision card) | **Offer Season** (page), **Signed & Sealed** (vision card), **The Yes List** (Strong matches) with the subtitles from the prototype |
| First name | `contact.first_name`; omitted when missing/placeholder or `vision.use_first_name: false` |
| Preview button | Kept ("preview: the moment"), shown only in forest themes |
| Pages | Static radar, live radar, Applications page |

## 3. Architecture

Approach A: theme assets are real files, injected via template tokens; one small pure module
resolves everything.

```
config/settings.yaml  radar.theme ─┐
render_radar.py --theme ───────────┼─► src/job_hunter/theme.py ─► ResolvedTheme(name, boot, css, js, config_json)
candidate_profile.yaml vision/contact ┘                                   │
                                                                          ▼
scripts/render_radar.py  (selection_render_kwargs → render)  ──► __THEME_BOOT__ / __THEME_CSS__ / __THEME_JS__
scripts/render_applications.py (same helper)                  ──► same three tokens in applications_template.html
```

### 3.1 New / changed units

| Unit | Responsibility | Depends on |
|---|---|---|
| `src/job_hunter/config.py` | `RadarConfig(theme: Literal["auto","forest","forest-dawn"] = "auto")` on `Settings.radar`; `VisionConfig(role, pay_note, start_note, use_first_name=True)` on `CandidateProfile.vision` (optional, defaults = prototype text) | pydantic |
| `src/job_hunter/theme.py` (new, pure) | `resolve_theme(cli, settings)`; `build_vision(profile)` → dict; `load_theme(name, vision)` → `ResolvedTheme`; JSON escaping for the config block | config, the asset files |
| `scripts/templates/themes/forest.css` | night palette variables, backdrop, manifestation components, celebration overlay | — |
| `scripts/templates/themes/forest-dawn.css` | sunrise sky/sun/rays + palette overrides, layered after `forest.css` | forest.css |
| `scripts/templates/themes/offer.js` | builds the backdrop for the active theme, applies titles/affirmations/card/subtitles, offer-mode switch, celebration + preview button, listens for the live event | config JSON |
| `scripts/templates/radar_template.html`, `applications_template.html` | gain `__THEME_BOOT__` (head), `__THEME_CSS__` (end of `<style>`), `__THEME_JS__` (end of body); nothing else changes | — |
| `scripts/render_radar.py` | `selection_render_kwargs` and `render()` accept/compute the resolved theme and fill the three tokens (empty strings for `auto`); `--theme` flag | theme.py |
| `scripts/render_applications.py` | fills the same tokens | theme.py |
| `scripts/templates/radar_live_ui.js`, `applications_ui.js` | dispatch `jobhunter:application-status` after the server acknowledges a status change **to `offer`** | — |

`scripts/serve_radar.py` already renders through the same kwargs path, so it inherits the theme
with no extra flags. The pipeline's radar subprocess reads `settings.yaml`, so it needs none either.

### 3.2 Data contract (config JSON → `offer.js`)

```json
{"theme": "forest-dawn", "firstName": "Jane" | null,
 "role": "Your next role", "pay": "Better. Higher paying.", "when": "Start date: soon"}
```

- Embedded as `<script type="application/json" id="mf-config">`, with `<`, `>`, `&` and U+2028/9
  escaped so no value can end the script element.
- `offer.js` writes **every** dynamic string with `textContent` / `createTextNode`. The prototype's
  `innerHTML` use for the name and role is explicitly removed.
- `firstName` is `null` when `contact.name` is empty/placeholder (`contact_problems`-style check)
  or `use_first_name` is false; the JS then uses the no-name affirmation variants.

## 4. Behaviour

### 4.1 Page changes (client-side, forest themes only)

- Masthead: `<h1>` becomes **Offer Season** + tagline "Open season on the right role."; the data
  sentence under it is untouched. Daily affirmation line below it (first shown line is a
  time-aware greeting; click cycles).
- Vision card **Signed & Sealed** ("Your next role, already in the envelope.", `Dear {name},`).
- Strong matches heading becomes **The Yes List** (the `score ≥ 75 · N jobs` count stays).
- Section subtitles, empty-state suffix ("All clear — nothing standing between you and the offer."),
  closing line.
- **Offer mode switch** (bottom-left, persisted in `localStorage["job-hunter-offer-mode"]`): off
  hides every manifestation element and restores each swapped label from its saved original text.
- Backdrop is static (pines, ground fog, a few twinkling motes; moon for night, sun/rays/stars for
  dawn); `prefers-reduced-motion` removes all animation.
- Rows, stat tiles and filters stay on flat opaque surfaces; for `forest-dawn` the text column sits
  on a dark scrim so no text renders over the bright horizon.

### 4.2 The celebration

`window` event `jobhunter:application-status` with `detail: {status, sourceKey, jobId}` is
dispatched by the live scripts only after a server ack of a status change to `offer`. `offer.js`
shows "You got the offer, {name}." with rising motes for ~5 s (click to dismiss). The
**preview: the moment** button triggers the same overlay manually.

### 4.3 Compatibility

- `auto` (default): `ResolvedTheme` has empty `boot/css/js`, so rendered HTML is identical to today.
- Existing assertions on titles/labels in tests keep passing because swaps happen in the browser,
  never in generated markup.
- Theme CSS is injected after the template's `prefers-color-scheme: dark` block; with equal
  specificity the later rule wins, so `forest` overrides an OS dark setting.

## 5. Error handling

- Unknown theme in YAML → pydantic validation error naming the allowed values (same as other settings).
- Unknown `--theme` → argparse `choices` error.
- Missing/corrupt `localStorage` → defaults to offer mode on (all access wrapped in try/catch).
- No `contact` block → no first name, generic wording; never an error.
- Missing theme asset file → `FileNotFoundError` at render time (fails loudly, like adapters).

## 6. Testing

- `tests/test_config.py`: `radar.theme` default/valid/invalid; `vision` defaults and overrides.
- `tests/test_theme.py` (new): precedence CLI > settings > auto; first-name rules (missing,
  placeholder, `use_first_name: false`, multi-word, honorifics not stripped); config-JSON escaping
  with hostile strings (`</script>`, `&`, quotes, U+2028); `auto` returns empty parts.
- `tests/test_render_radar.py`: **golden** — `auto` output byte-identical to a pre-change
  baseline captured on this branch before any edit; `forest` and `forest-dawn` contain the three
  token expansions, one `mf-config` block, and exactly one copy of the CSS/JS; live mode too.
- `tests/test_render_applications.py`: same token checks.
- `tests/js/`: `node --test` for the status-event dispatch (fires only on change → `offer`).
- `tests/test_forest_theme_browser.py`: Playwright smoke, skipped when Chromium is missing —
  title swap, restore on offer-mode off, celebration shows, no console errors, for both themes.
- Fixtures use fake names only (`Jane Doe`); `test_no_owner_pii_in_shareable_files` must still pass.

## 7. Documentation

README (themes section + refreshed screenshots), `docs/USAGE.md` (settings, `--theme`, `vision:`),
`docs/SPEC.md` (render tokens, event contract), example blocks in `config/settings.yaml` and
`config/candidate_profile.example.yaml` (fake values). No `SKILL.md` content changes, so no skill
version bumps.

## 8. Privacy note

The first name appears in locally generated report HTML (git-ignored paths), so a shared screenshot
would show it. `vision.use_first_name: false` removes it; the offer-mode switch hides all
manifestation text without a re-render.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Palette hurts score-band readability | keep the five-band ramp distinct; contrast check recorded in the plan |
| Template edit breaks existing tests | tokens only; `auto` golden test written first |
| Prototype files live in git-ignored `brag-output/` | plan copies them into `scripts/templates/themes/` as the tracked source |
| Live scripts regress | event dispatch is additive; node tests + existing `test_radar_live_js.py` |
