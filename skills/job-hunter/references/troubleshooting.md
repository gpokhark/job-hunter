# Troubleshooting

Run `uv run job-hunter doctor`, then `uv run job-hunter source-status`. Test one source with `uv run job-hunter source-test <key>`.

A failed source is not zero openings. Schema and endpoint failures remain isolated and are recorded in SQLite. A suspicious count drop retains old active postings. Update only that company's `config` or its adapter when a public first-party response shape changes.

Collection is HTTP-only (httpx) for every source except the `stealth_html` adapter, which
uses a real headless stealth browser (Scrapling's `AsyncStealthySession`) against sites whose
content only exists after client-side JS runs, or (for `astemo` specifically) that sit behind a
Cloudflare/Akamai-style bot-management challenge. Before assuming a Cloudflare-blocked site needs
this: check whether it has an unprotected backend, the way GM's did — GM's public front end
(Findly-branded, Cloudflare-protected) looked identical to Astemo's until a real job link
revealed its actual candidate-facing system was a public Workday API with zero bot protection,
and it moved off `stealth_html` entirely. For a source where the block really is the only path
in, using this adapter crosses into deliberately defeating a site's own anti-automation controls
— a deliberate, disclosed choice for this project, not an oversight — and carries real ToS
exposure independent of intent, plus a real per-request cost (a browser launch/page load, not a
lightweight HTTP call). Reach for it only when no plain anonymous endpoint exists; prefer every
other adapter first. It requires the optional `stealth` extra
(`uv sync --extra stealth && uv run scrapling install`), which is not installed by default.

