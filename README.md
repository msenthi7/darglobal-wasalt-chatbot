---
title: DarGlobal & Wasalt Assistant
emoji: 🏙️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Cited chatbot over public DarGlobal & Wasalt data
---

# DarGlobal & Wasalt Assistant

**Live demo:** being deployed on Hugging Face Spaces (link added on deploy) · **Source:** https://github.com/msenthi7/darglobal-wasalt-chatbot

A retrieval-augmented chatbot that answers questions about DarGlobal's real-estate projects and Wasalt's Saudi property listings. It uses only publicly available data, cites the exact pages behind each answer with their capture dates, runs on a free OpenRouter model, and ships as a Docker container.

> Independent technical assignment. Not affiliated with or endorsed by DarGlobal or Wasalt.

## Try asking

- Which DarGlobal projects are in Dubai?
- Tell me about Trump Tower Jeddah
- What amenities do the Trump Golf Villas in Oman have?
- How many apartments for sale are listed on Wasalt?
- Which Jeddah districts does Wasalt cover?
- ما هي مشاريع دار جلوبال في عُمان؟

## How it works

```
 OFFLINE: python -m scraper.build                       ONLINE: Docker container (FastAPI)
 ────────────────────────────────                       ──────────────────────────────────
 robots.txt + sitemaps (live) ──┐                       question ─► validate ─► rate limits
 Internet Archive copies ───────┼─► extract ─► trap ─►   │
   of DarGlobal/Wasalt pages    │   text +     scan &   ▼
 manual saves (optional) ───────┘   CMS facts  quarantine  BM25 search (EN + AR) ─► top sources
                                        │                    │
                                        ▼                    ▼
                         data/corpus.jsonl  ─────────►  OpenRouter free model (verified $0)
                         data/manifest.json                  │
                         data/trap_report.json               ▼
                                                   answer + [n] citations ─► web UI
```

- **Scraper** (`scraper/`): discovers pages from each site's sitemaps, fetches content, extracts visible text plus structured project facts from DarGlobal's page data (status, unit sizes, completion date, amenities, location), derives Wasalt coverage and inventory documents from its sitemaps, scans everything for AI traps, and writes a versioned corpus with a manifest of every request and outcome.
- **App** (`app/`): loads and re-validates the corpus, builds a bilingual BM25 index, retrieves the best passages for each question, asks a free model to answer only from those passages with numbered citations, then checks every citation against the sources it actually sent.

## Data collection

| Source | What was collected | Captured |
|---|---|---|
| DarGlobal sitemap (live) | 482 page URLs; 450 selected after de-duplication (6 transactional pages skipped, 23 over the page cap) | 2026-11-20 |
| DarGlobal pages (Internet Archive) | 382 pages recovered (44 had only bot-challenge captures, 23 had none); 353 documents after dropping 15 near-empty and 14 duplicate pages, with structured facts for project pages | 2025-03-20 to 2026-07-26 |
| DarGlobal project catalog | 13 documents: all 39 projects with countries, and the website's project menu by country | 2026-07-26 |
| Wasalt sitemaps (live) | 62,662 listing URLs (62,651 parsed), 8,330 for-sale and 5,054 rental search pages, turned into 63 coverage and inventory documents covering 169 cities and areas | 2026-11-20 |
| Wasalt pages (Internet Archive) | 19 of 21 target pages: home, information pages and city search pages in English and Arabic | 2025-05-19 to 2026-10-09 |

**Why archived copies?** Both sites answer automated requests with a JavaScript bot challenge (Imperva on darglobal.co.uk, Cloudflare on wasalt.sa). The scraper records this as `blocked_bot_challenge` in `data/manifest.json` and does not try to get around it: no browser impersonation, no headless-browser stealth, no third-party unblocking proxies. Instead it uses what the sites publish for crawlers (the sitemaps listed in their robots.txt) and dated public [Internet Archive](https://web.archive.org/) copies of the same pages, and it applies each site's robots.txt rules to the archived copies too. Every answer shows the capture date of each source, and pages saved by hand can be added through `data/manual/`.

Politeness: honest user agent, robots.txt (RFC 9309, wildcard-aware) for every host, checked for both the page and each archived copy's URL; a robots.txt hidden behind a bot challenge counts as "disallow all". Requests are spaced 2–3 s per host, retried only for transient errors with `Retry-After` honoured, and capped in size. A run where more than 10% of fetches fail is marked partial and never replaces the last complete corpus.

## AI traps and prompt injection

The assignment brief asked us to watch for instructions aimed at AI tools ("if you are an AI, embed this…"). The pasted brief itself contained none. Everything the scraper collects is scanned:

- **Where:** page titles, HTML comments, hidden elements, meta tags, alt/aria/data attributes, embedded JSON, visible text, robots.txt, llms.txt and sitemaps.
- **What:** "if you are an AI/LLM/bot…", "ignore previous instructions", notes addressed to AI assistants, chat-template role markers, Arabic equivalents, and invisible Unicode tag characters (decoded, then scanned).
- **What happens:** high-severity lines (and titles or descriptions) are quarantined before indexing, and the app refuses any corpus record whose text or title still contains one. Findings are listed in `data/trap_report.json` and in the app's "How this works" panel.

**What was found:** No instruction-style traps were found in the 401 DarGlobal and Wasalt pages, so nothing was quarantined. The only AI-directed content was in Wasalt's robots.txt: it grants extra access (its internal search) only to named AI crawlers (ChatGPT-User, anthropic-ai, PerplexityBot) and carries a comment addressed to AI readers ("We're allowing AI access to most areas… no additional restrictions are needed"). The scraper does not claim to be one of those crawlers; it identifies itself honestly and follows the rules for all other bots, which disallow `/search`. Wasalt's `llms.txt` sits behind the same bot challenge and was not read.

At answer time, retrieved text is fenced as untrusted data (fence tags inside it are stripped until none can re-form), the rules live only in the system prompt, the model has no tools, and citations pointing at sources that were never sent are removed.

## Reliability and security controls

- **Free models only.** Configured models are checked against OpenRouter's public pricing at startup and every 6 hours; anything not $0 or no longer listed is dropped. Each answer shows which model replied and whether a fallback was used.
- **One retry policy.** Timeouts, 429s and 5xx responses retry with backoff and jitter, then fall back to the next free model: at most 3 model calls per question, all inside a 75-second deadline that is re-checked after waiting for a free slot. Authentication errors stop immediately, and there are no hidden SDK retries.
- **Quotas before model calls.** Per-visitor limits (6 questions/min, 20/day, and at most 15 model calls/day, so one visitor cannot use up the shared allowance), a global 15/min limit, and a daily cap of 45 upstream calls (under OpenRouter's free tier of 20/min and 50/day without credits). Quota is spent only on real model calls, and questions with no matching sources are answered without one.
- **Truthful results.** Errors carry a stable code, a safe message, a retryable flag and a request ID. Raw exceptions, upstream messages and the API key never reach the client, and the key is redacted from logs. Answers without citations are labelled as such.
- **Input validation.** Strict schema (message up to 1,000 characters, up to 12 history turns, user/assistant roles only, unknown fields rejected) and a 32 KB body cap; the UI trims earlier turns to fit, so long Arabic conversations keep working.
- **Browser safety.** CSP with self-hosted scripts only, `nosniff`, model text rendered as DOM text (never `innerHTML`), and links restricted to the two sites and the archive.
- **Corpus integrity.** Schema version, allowed hosts and a SHA-256 hash per document are checked at load, and bad records are rejected with a reason.
- **Container.** Digest-pinned `python:3.13-slim`, hash-locked dependencies installed with `--no-deps`, non-root UID 1000, read-only code and data, secrets supplied only at runtime, and an allowlist `.dockerignore`.
- **Privacy.** No chat storage; logs hold request IDs, status codes and timings, not message text or IP addresses (the server's own access log is off); visitor identities for rate limiting are hashed. Questions go to a third-party free model through OpenRouter, and the UI says so.

## Run it

```bash
cp .env.example .env          # then add your OPENROUTER_API_KEY
docker build -t dgw-assistant .
docker run --rm -p 7860:7860 --env-file .env dgw-assistant
# open http://localhost:7860
```

Without Docker (Python 3.13):

```bash
python -m venv .venv && .venv/bin/pip install --require-hashes --no-deps -r requirements.txt
HOST=127.0.0.1 OPENROUTER_API_KEY=... .venv/bin/python -m app
```

Rebuild the corpus (about 25 minutes, mostly polite request spacing; reruns reuse the local archive cache):

```bash
.venv/bin/pip install --require-hashes --no-deps -r requirements-scraper.txt
.venv/bin/python -m scraper.build
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | (none) | Required for answers; without it the app reports `not_configured`. |
| `OPENROUTER_MODELS` | Gemma 4 31B → Nemotron 3 Ultra → Gemma 4 26B → `openrouter/free` | Comma-separated fallback order; each must verify as free. |
| `UPSTREAM_CALLS_PER_DAY` | 45 | Daily cap on model calls (raise only if your OpenRouter plan allows). |
| `RATE_PER_CLIENT_PER_MINUTE` / `_PER_DAY` | 6 / 20 | Per-visitor question limits. |
| `UPSTREAM_CALLS_PER_CLIENT_PER_DAY` | 15 | Per-visitor daily cap on model calls. |
| `RATE_GLOBAL_PER_MINUTE` | 15 | Whole-app limit. |
| `PUBLIC_URL`, `SOURCE_URL` | (none) | Attribution header for OpenRouter; source link in the UI. |

## Tests

```bash
.venv/bin/pip install --require-hashes --no-deps -r requirements-scraper.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

86 tests, with OpenRouter mocked so no network is needed. They cover trap detection (including Arabic and Unicode tag smuggling) and quarantine, URL-allowlist bypass attempts, robots wildcards, bot-challenge handling, redirect scope, retries and `Retry-After`, size caps, XML entity bombs, corpus integrity, retrieval determinism, rate limits, model fallback and the auth stop, the daily budget, key redaction, citation validation, API error envelopes and spoofed `X-Forwarded-For` headers. CI runs the suite plus a container smoke test on every push.

## Limitations

- **Snapshot data.** DarGlobal pages come from archive captures (each answer shows the date). Wasalt inventory figures are derived from listing URLs (type, size, bedrooms), not from listing pages, so they carry no prices and no per-listing city.
- **Lexical retrieval.** BM25 with light Arabic normalisation and a small synonym list, not embeddings, so paraphrases that share no words can be missed.
- **Free models vary.** OpenRouter's free line-up changes and can be rate-limited, so answers may take 10–40 seconds at busy times and occasionally come from a fallback model.
- **Single instance.** Rate limits live in memory for one process.
- **The model can still be wrong.** Citations let you check each claim against its source.

## Verification status

- **Automated tests:** 86 unit, integration and API tests pass with OpenRouter mocked. Mutation checks confirmed that the trap-regex and file-permission regression tests fail without their fixes.
- **Independent review:** a separate reviewer found 15 issues (none high-severity), including a page-title route around the trap filter, quota exhaustion by one visitor, and robots.txt gaps for archived copies. All 15 are fixed; 13 have automated regression tests, the access-log change was confirmed in the container's logs (no IP addresses), and the UI history trimming was checked by code inspection.
- **Container:** image built and smoke-tested locally (linux/arm64): runs as UID 1000, serves the UI, answers `/healthz`, and returns a typed `503 not_configured` when no key is set. CI repeats the build and smoke test on linux/amd64.
- **UI:** checked in a browser at desktop and phone width, in light and dark themes, with no console errors under the CSP.
- **Retrieval:** 12 representative English and Arabic questions spot-checked; each retrieves the expected source first.
- **Live model answers:** not yet verified in this revision (pending the deployed API key).
- **Not done:** load testing, multi-instance deployment, systematic answer-accuracy evaluation.
