# Discovery playbook: known platform signatures and probing techniques

Concrete signatures and commands for [../SKILL.md](../SKILL.md)'s Phases 1–4. Every pattern below
was found and verified against a real site during this project's development — treat this as a
"check these first" list, not an exhaustive taxonomy. Always re-verify against the actual site;
don't assume a pattern applies without confirming it live.

## Quick probing tools

- `scripts/endpoint_probe.py <url>` — fetches a URL and prints status/content-type/a text sample.
  Use it (or plain `curl`) before writing any adapter config.
- A one-off Python snippet with `httpx` (already a project dependency) for anything requiring
  header/param manipulation or JSON parsing.
- Scrapling, for one-time rendering only, via
  `uv run --with "scrapling[fetchers]" python3 -c "..."` (doesn't touch the project's own
  dependency list) — or the project's own `stealth_html` adapter if the `stealth` extra is
  already installed (`uv sync --extra stealth`).
- A short Playwright script when you need to see the *real XHR/fetch calls* a JS-heavy page makes,
  not just its rendered HTML — register a `page.on("request"/"response", ...)` handler, `goto` the
  page, and print every request whose `resource_type` is `xhr`/`fetch`. This is exactly how
  Stellantis's ADP token handshake and Apple's hydration-data endpoint were both found: render
  once, read what the browser actually asked for, then replay that with plain httpx.

## Known ATS/platform signatures

**Workday** — URLs shaped `https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs`.
A POST to that URL with a JSON body (`{"limit": N, "offset": N, "searchText": ""}`) returns a JSON
listing with `postings` (each with `title`, `externalPath`, `postedOn` — a *relative* string like
"Posted Today"/"Posted 3 Days Ago", parsed by `normalizer.parse_relative_posted`) and `total`. No
auth needed. This project's `workday.py` supports this natively via `workday_native: true`.

**Oracle HCM (Fusion Recruiting Cloud)** — URLs shaped
`https://<tenant>.fa.<region>.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&finder=findReqs;siteNumber=<SITE>,...`.
Public, no auth. Has a per-request page-size cap well below some tenants' real job count — check
`TotalJobsCount` in the response against how many rows actually came back; if capped, use
`paginate: true` + `total_path` (see `oracle_hcm.py`) to loop `offset`. **Not guaranteed to be
sorted by posting date** — verified for at least one tenant that it isn't (dates jump around
non-monotonically across pages); don't add early-pagination-stop without checking this tenant
specifically.

**Lever** — `https://api.lever.co/v0/postings/<tenant>?mode=json`. Public JSON, `createdAt` is an
epoch-millis posted date. Thin `ConfigurableJsonAdapter` subclass (`lever.py`).

**Phenom People** — the visible search-results page eager-loads the current page of results into a
plain (non-escaped) JSON object for SEO, keyed by something like `"eagerLoadRefineSearch"`, located
by brace-matching from its opening `{` (there's no reliable closing-brace count in the surrounding
markup — see `phenom.py`'s `_extract_embedded_json`). Pagination is via `from` (row offset) and `s`
(constant `"1"`) query params — the commonly-guessed `start`/`num` are silently ignored; the real
ones are typically only discoverable by rendering the page once and reading its own generated
pagination links. **The listing blob's `postedDate` and the detail page's JSON-LD `datePosted` can
disagree** (verified for one Phenom site: the detail-level field drifted toward "today" over time
while the listing-level field stayed fixed) — prefer the listing-level field unless you've checked.

**SuccessFactors Recruiting Marketing (RMK)** — plain server-rendered HTML table, one `<tr>` per
job. Fits `html_paginated`/`successfactors_rmk` with `card_selector`, `link_selector`,
`title_selector`, `location_selector`, and (if present) `posted_at_selector` targeting a
`td.colDate span.jobDate`-shaped element, parsed via `normalizer.parse_display_date`.

**ADP Recruiting Management (RM)** — a career-site front end at
`https://myjobs.adp.com/<career_site_domain>/cx/...` that never renders anything server-side. A
public, unauthenticated `GET /public/staffing/v1/career-site/<domain>` (on `myjobs.adp.com`)
returns a `myJobsToken`, which the front end replays as a `myjobstoken` request header on
`GET https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters`
(OData-style `$top`/`$skip`/`$select`/`$filter` params) — this single endpoint returns full
description, qualifications, and a `postingDate` per requisition, no separate detail fetch needed.
Confirmed reliably sorted newest-first for at least one tenant (worth re-verifying per tenant before
relying on it for early-stop). Found via the Playwright network-capture technique above, not by
guessing. See `adp_recruiting.py`.

**Next.js / Nuxt / React Router SSR hydration data** — many modern SPA career sites still
server-render a full JSON snapshot of the page's data into the initial HTML, even though the
*visible* content only appears after JS runs — look for `<script id="__NEXT_DATA__" type="application/json">`,
`window.__NUXT__ = {...}`, or (React Router) `window.__staticRouterHydrationData = JSON.parse("...")`.
The React Router form is double-encoded: the argument to `JSON.parse` is itself a JSON-escaped
string, so it needs one `json.loads` to unescape the JS string literal, then a second `json.loads`
on the result. See `apple.py`'s `_extract_hydration_data` for the exact pattern. A plain `curl`/httpx
GET already returns this — confirmed for at least one site that this fully replaces an assumed need
for a stealth browser (the site wasn't bot-blocked at all, just assumed to be JS-only).

**schema.org `JobPosting` JSON-LD** — a cross-platform SEO convention, unrelated to any one ATS
vendor: `<script type="application/ld+json">{"@type": "JobPosting", "datePosted": ..., "description": ...}</script>`
on a job's *detail* page. `normalizer.extract_job_posting_ld` finds this regardless of where in the
tag's attributes `type=` appears (some platforms put another attribute first) and regardless of
whether it's a bare object or wrapped in `"@graph"`. `html_paginated.py`'s `fetch_detail` already
checks for this unconditionally as a fallback — a new `html_paginated`-family source gets this for
free with no extra config.

**Liferay DDM (Dynamic Data Mapping) portals** — recognizable via `/o/liferay-*-theme/` asset paths
or a `com_liferay_dynamic_data_mapping_form_web_portlet` instance in the markup. No JSON-LD;
instead an inline `<script>` sets a JS object literal like
`var JobOfferData = {id: "...", name: "...", publicationDate: "Mon D, YYYY H:MM:SS AM/PM"}`, used to
populate an application-confirmation email — the site's own authoritative publish date, despite the
unusual format. See `normalizer.parse_liferay_publication_date`.

## Verifying a claimed hidden backend is real, not a guess

1. Request it directly with plain httpx/curl, **no cookies, no session state carried over from a
   browser** — a source that only works with a browser-issued cookie isn't actually a plain
   anonymous endpoint.
2. Confirm the sample job (by title or ID) actually appears in the response.
3. Confirm pagination is real by changing the offset/page param and diffing the results — not by
   assuming a parameter name works because it looks reasonable.
4. If a token/handshake is involved (ADP's `myJobsToken` pattern), confirm the token comes from a
   **public, unauthenticated** call — not from a login flow — before treating it as a "no
   credentials needed" endpoint.

## When `stealth_html` is (and isn't) the right call

Use it only when Phase 2 (find the real backend) has genuinely turned up nothing — either the
content really doesn't exist until client-side JS runs (no hydration data, no discoverable API), or
the site actively bot-blocks every plain request (Cloudflare Turnstile, Akamai) and no unprotected
backend exists behind it. Every time this project defaulted to assuming `stealth_html` was needed
without first checking Phase 2, that assumption turned out wrong (GM, Stellantis, Apple all had a
plain backend). When it genuinely is needed, say so explicitly and disclose the ToS tradeoff — see
existing `companies.yaml` entries for `astemo`/`google` for the expected tone.
