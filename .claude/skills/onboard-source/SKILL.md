---
name: onboard-source
description: Onboard a new employer career site into job-hunter — identify its real scraping mechanism, wire it up in config/companies.yaml (reusing an existing adapter whenever possible), test it, verify it live, and update README.md/CLAUDE.md. Use when the user gives a careers listing URL and a sample job URL and asks to add/onboard a new company or source.
---

# Onboard a new source

This is a repo-maintenance skill for `job-hunter` itself, not the end-user job-search skill
(that one is `skills/job-hunter`). Use it when asked to add a new employer/company as a source.

## Required inputs

- Company name.
- The careers **listing/search** page URL.
- One real **sample job detail** page URL from that same site (used throughout to verify every
  claim against a concrete job, not a guess).

If either URL is missing, ask for it before starting — don't guess a URL.

## Ground rules (apply throughout, not just at the end)

- **Verify every claim against a real HTTP response, not a guess.** Every adapter and every piece
  of documentation in this project was built by fetching the actual site and reading the actual
  response — never invent an endpoint shape, a query parameter, or a field name.
- **Prefer reusing an existing adapter via `config/companies.yaml` alone.** Only write new adapter
  code when the platform genuinely doesn't fit any existing shape (see Phase 5).
- **Plain httpx first, always.** `stealth_html` (a real headless browser) is a deliberate,
  disclosed exception for a site with no other viable path — not a default. Reaching for it before
  ruling out a plain endpoint has been wrong more often than right in this project (GM, Stellantis,
  and Apple all turned out to have an unprotected plain endpoint behind what looked like a
  JS-only or bot-blocked front end).
- **Fail loudly, don't fake data.** If no plain endpoint and no viable `stealth_html` path exists,
  the company becomes `adapter: unsupported` with a specific `unsupported_reason` — never a
  fabricated config that "sort of" works.
- **Disclose tradeoffs in writing.** Partial coverage, ToS exposure from `stealth_html`, an
  unverified/unstable date field, an unsorted listing — write these down in both the
  `companies.yaml` comment and the README, the way every existing entry does.

See [references/discovery-playbook.md](references/discovery-playbook.md) for the concrete
platform signatures (Workday, Oracle HCM, Lever, Phenom, SuccessFactors, ADP RM, Next.js/Nuxt/React
Router hydration data, JSON-LD, Liferay DDM, etc.), the exact commands used to probe them, and the
verification techniques (date-field stability, pagination sort-order) referenced below.

## Procedure

### Phase 1 — Plain-fetch discovery (always first)

1. `curl`/`httpx` both URLs with a normal browser `User-Agent`, no cookies, no auth. Note status
   code and whether the response is a real page or a challenge/empty shell.
2. Search the raw listing HTML for the sample job's title or ID. If it's there, the listing may
   already be scrapable with plain HTML selectors or an embedded JSON blob — check for:
   - A `<script type="application/ld+json">` schema.org `JobPosting` block (a common,
     platform-independent SEO convention — see `normalizer.extract_job_posting_ld`).
   - A framework hydration payload (`__NEXT_DATA__`, `__NUXT__`,
     `window.__staticRouterHydrationData`, or a bare eager-loaded search-results object) — these
     often carry full listing data server-side even when the visible page looks JS-rendered.
   - A bespoke inline JS object literal (e.g. Liferay's `JobOfferData`) — check the detail page too,
     not just the listing.
3. If the raw HTML is a real page but the job data genuinely isn't in it anywhere, this site's
   content likely only exists after client-side JS runs — don't stop here, go to Phase 2 before
   concluding a browser is required.

### Phase 2 — Find the *real* backing system

This is the single highest-value step in this project's history — the visible careers page is
very often just a skin (Radancy/TalentBrew, Findly, a custom Angular/React SPA, ADP's `myjobs.adp.com`
front end) over a completely different, often-public backend.

1. Follow the sample job's real "Apply"/detail link and look at where it actually goes — the host,
   not just the path. Compare it against known ATS URL shapes in the playbook (Workday
   `*.myworkdayjobs.com/wday/cxs/...`, Oracle HCM `*.oraclecloud.com/hcmRestApi/...`,
   `api.lever.co`, `myjobs.adp.com`/`my.adp.com`, etc.).
2. If it lands on a recognizably different platform, test that platform's typical listing/search
   endpoint directly with plain httpx (no cookies) — see the playbook for the exact request shape
   each platform expects. Confirm it's genuinely public (no login/session required) and returns the
   sample job somewhere in it.
3. If a listing endpoint is found, verify its pagination is real: change the offset/page parameter
   and confirm the results actually change. Several sites silently ignore the "obvious" parameter
   name (e.g. `start`/`num`) while a different, less obvious one (`from`/`s`, `$skip`/`$top`) is the
   real one — found only by testing, never assumed.

### Phase 3 — Escalate to a browser only if truly necessary

Only after Phase 2 turns up nothing usable:

1. Render the listing (and, if needed, the detail) page once with Scrapling
   (`uv run --with "scrapling[fetchers]" python3 -c "..."`, or a quick Playwright script if you
   need to capture the real XHR/fetch calls the page makes — this is exactly how Apple's and
   Stellantis's real backing APIs were found: render once, read the network requests, then replay
   them with plain httpx).
2. If that reveals a plain endpoint after all, use it — a browser is a one-time discovery tool
   here, essentially never a runtime dependency.
3. If the site is genuinely bot-blocked (Cloudflare Turnstile, Akamai, etc.) with no unprotected
   backend found, and rendering through `stealth_html` reliably gets past it, that adapter is the
   right, disclosed choice — but say so explicitly in the `companies.yaml` comment and the README
   (real ToS exposure, not solved by "it's public data").
4. If even `stealth_html` can't get past it (e.g. an IP/ASN-level block, not a JS challenge), or if
   there's a structural reason no filter exists (e.g. no legal-entity facet), the company is
   `adapter: unsupported` with a specific `unsupported_reason`.

### Phase 4 — Verify the date field(s) and, if relevant, sort order

1. Identify every place a posting date might live (listing-level field, detail-level field, JSON-LD
   `datePosted`). If more than one exists, fetch both live for the *same* sample job and compare —
   don't assume they agree. (Honda's Phenom listing `postedDate` and detail JSON-LD `datePosted`
   were found to disagree; the detail-level field drifted toward "today" rather than staying fixed.)
   Prefer whichever field is verified stable across a re-check.
2. Only if you intend to add early-pagination-stop for this source (skip fetching pages once
   postings are past the recency cutoff — see `apple.py`/`adp_recruiting.py`): sample the listing
   across several widely-spaced offsets/pages and confirm dates decrease monotonically the whole
   way. If they don't (Ford/DENSO's Oracle HCM listing doesn't), do not add early-stop — it would
   silently drop real recent postings, not just cost more time. This is optional; don't add it
   unless the catalog is large enough to matter.

### Phase 5 — Choose or write the adapter

Prefer reusing an existing adapter via `config/companies.yaml` config alone:

- `workday` (native CXS API) · `oracle_hcm` · `lever` · `phenom` · `successfactors_rmk` ·
  `html_paginated` (generic CSS-selector HTML, with an automatic JSON-LD `posted_at` fallback) ·
  `html_multi_index` (same, but loops multiple listing URLs) · `adp_recruiting` · `json_api`'s
  `ConfigurableJsonAdapter` for a generic REST listing with a `fields` mapping.

Only write new adapter code (`src/job_hunter/adapters/<name>.py`) when the platform genuinely
doesn't fit any of these. If you do:

- Subclass `JobAdapter` (`adapters/base.py`); use `self.request()` (retry/backoff already built in)
  rather than a raw client call.
- Reuse `_date`/`_stringify` from `json_api.py` and `normalize_text` from `normalizer.py` instead
  of reimplementing them.
- Register the new class in `adapters/__init__.py`'s `ADAPTERS` dict.
- If it needs pagination and a stable date field, consider early-stop (Phase 4.2) and thread
  `self.max_posting_age_days` (already available on every adapter) — see `apple.py` for the pattern.

### Phase 6 — Wire it into `config/companies.yaml`

Add the entry in the same heavily-commented style as the rest of the file: what the visible front
end is, what was actually found (real backend, pagination params, date field), and why this
approach — a future reader (human or agent) should be able to trust the comment without
re-deriving it.

### Phase 7 — Tests

- If new adapter code was written, add a respx-mocked fixture test in `tests/test_adapters.py`
  following the file's existing patterns (construct a minimal fake response shaped like the real
  one, assert the parsed `JobSummary`/`JobDetail` fields).
- If a new company key was added, bump the count assertion in `tests/test_config.py`
  (`assert len(companies) == N ...`).
- Run `uv run pytest -q` and `uv run ruff check .`; fix until both are clean.

### Phase 8 — Live verification

- `uv run job-hunter source-test <key>` — confirms connectivity and a real job count.
- `uv run job-hunter search --companies <key> --include-seen --refresh-details --output /tmp/<key>_check.json`
  — confirms summaries, details, location, and date all populate sanely. Spot-check the original
  sample job by title/ID in the output.

### Phase 9 — Documentation

- **`README.md`**: add/update the company's row in the "Source status" table (Status / Collection /
  Posted date? / Active-closed detection? / Tools used — match the table's existing terse-but-
  specific style, citing the real mechanism found, not a generic description). Update the "Posted
  date" summary counts/bullets if this source contributes a mechanism. If new bespoke adapter code
  was written, add a `### The `<adapter>` adapter` section mirroring the existing `apple`/
  `adp_recruiting` ones. Update the `stealth_html` company list (and `cli.py`'s `doctor()` message)
  if this source uses or drops that adapter.
- **`CLAUDE.md`**: add the new adapter filename to the `adapters/` bullet's file list if new code
  was written. Add a short note only if a genuinely new, reusable *technique* was discovered (a new
  hydration-data shape, a new date-field trap) — CLAUDE.md deliberately does not enumerate every
  company, only load-bearing architecture and reusable lessons.

### Phase 10 — Report back

Summarize plainly: what backend was actually found (and what the visible front end turned out to
be, if different), which adapter/config was used, the live job count, a location/date spot-check
against the original sample job, and any caveats honestly (partial coverage, ToS exposure from
`stealth_html`, an unstable date field, etc.) — this project's whole documentation style is
"disclose the tradeoff," not "claim it's solved."
