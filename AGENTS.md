# AGENTS.md

This file gives coding agents working in this repository the minimum context they need to make safe changes without reverse-engineering the whole project first.

## Project Overview

Connection Machine is a LinkedIn automation worker built around a persistent browser session, a SQLite-backed task queue, and OpenRouter-powered text generation.

Today the codebase supports three practical automation flows:

- Sending LinkedIn connection requests from queued database tasks
- Creating LinkedIn posts from queued database tasks
- Generating feed comments autonomously when the account is idle and an OpenRouter key is configured

The worker uses the official Python Playwright client and its pinned WebKit build. Only the Python scheduler and metrics stay resident. Each poll that needs browser work gets a fresh driver and persistent context; both close before the worker sleeps, including on errors. Consecutive tasks cannot accumulate tabs or driver state across polls. This reduces idle and long-running usage, **not** the memory needed to run one page.

## High-Level Runtime

```text
src/main.py
  -> load .env
  -> initialize database
  -> start TaskDispatcher without launching or authenticating a browser

TaskDispatcher
  -> polls pending DB tasks
  -> enforces spacing and per-day limits
  -> acquire an authenticated page only for eligible work, before claiming a queued task
  -> run invite/post handlers, notification discovery or autonomous feed comments
  -> record outcome; drop page references and close context and driver before sleep
```

## Main Components

- `src/main.py`: entry point, browser startup, LinkedIn auth recovery, debug CLI modes, graceful shutdown
- `src/dispatcher.py`: polling loop, task selection, spacing windows, zombie cleanup, autonomous comment scheduling
- `src/db.py`: SQLAlchemy engine, `linkedin_tasks` table, enums, DB initialization
- `src/invite_schedule.py`: deterministic rolling-quota slots and minimum invitation spacing
- `src/tasks/invite.py`: profile visit, connect-button discovery, required-by-default AI-generated note, invitation confirmation
- `src/linkedin_profile.py`: canonical target identity, shared topcard ownership, settled page text, and verbatim verification of LLM-parsed profile fields
- `src/invite_modal.py`: recipient-verified invitation dialogs and scoped note/send controls
- `src/connect_heuristics.py`, `src/connection_state.py`: identity-bound Connect/More actions, cache hints, and target connection-state detection
- `src/tasks/post.py`: opens the supplied LinkedIn composer URL and submits a post
- `src/tasks/comment.py`: scans the feed for safe posts, asks the LLM for a comment, optionally publishes it, and stores local comment history
- `src/llm.py`: OpenRouter calls for profile parsing, connection notes, feed comments, and connect-action detection
- `src/human_actions.py`: randomized sleeps, hover behavior, typing cadence, and click simulation
- `src/notifications.py`: optional outbound notifications

## Task Model

Database-backed task types in `src/db.py`:

- `send_invite`
- `create_post`
- `comment_feed_post`

Current dispatcher behavior:

- `send_invite` and `create_post` are handled as normal queued DB tasks
- `comment_feed_post` DB rows are treated as legacy and cleaned up
- feed comments now run autonomously from `TaskDispatcher.maybe_run_autonomous_comment()`

Task statuses:

- `pending`
- `processing`
- `completed`
- `failed`

## Operational Behavior

### Dispatcher cadence

- Poll interval is 10 seconds
- Cold startup does not import Playwright or HTTPX; after first use Python may retain the libraries, but not the browser/driver processes. Disabled metrics skip history and dashboard-count queries, not scheduling checks. Notification metrics cache only the last scan summary, keyed by the file revision; eligibility, cooldowns and deduplication still read authoritative state.
- Interrupted `processing` invitations are held as `failed` / `invite_not_confirmed` for reconciliation, never blindly replayed; other stuck task types are reset to `pending`
- stale pending `create_post` tasks older than one hour are deleted
- legacy DB-backed feed-comment tasks are deleted
- Runnable invitations take priority over other queued task types and notification scans
- New notification scans/comments do not start within 15 minutes of a pending invitation becoming eligible; already-running synchronous actions are not forcibly interrupted
- Notification discovery runs in available idle windows, with its existing 30-minute minimum interval. With recent invite history and a backlog, it runs at most once per quota event; it never runs during a durable invite cooldown or exhausted invite quota.
- `src/worker_watchdog.py` bounds startup and each dispatcher cycle to 15 minutes without a progress heartbeat. Untimed browser reads can otherwise stall the worker. The watchdog emits a stack and exits nonzero without waiting for browser cleanup; Docker's `restart: always` restarts the worker. It never retries an invitation; startup still quarantines interrupted `processing` invites.

### Invitation identity and personalization

- Every invite requires the queued URL, loaded profile, Connect target, and invitation dialog recipient to agree. Profile URLs are canonicalized; a canonical page link alone is not enough to prove the visible topcard belongs to the target.
- Profile fields come from one LLM parse per visit (`llm.parse_profile`) of the settled `<main>` text (unchanged for one second, at most eight seconds). The prompt asks only for the verified owner's headline, About, Experience, and follower/connection count phrases, never Activity, recommendations, or other people. `verify_profile_details` keeps a field only when every line appears verbatim in the page text, so the model cannot invent prose, employers, or counts.
- The structural extractor only establishes identity, the owner topcard, and action ownership. Classic profile cards and SDUI keyed cards inside a `LazyColumn` are supported; unkeyed single-child wrapper divs between the column and its topcard card are traversed (LinkedIn added them on 2026-11-02), while wrappers holding anything else are not.
- Personalization needs at least 80 characters of About or Experience and 200 characters of owner fields. Thinner profiles, or an unavailable parse, defer/skip the task before Connect with `profile_not_ready`; it never falls back to a note-less invitation.
- A nonempty note of at most 200 characters must be generated before Connect. Missing/invalid generation aborts with `llm_invalid_response`; there is no silent no-note fallback. The same preflight-generated note is used throughout the attempt, without regeneration after any potentially dispatched click. Note-less invitations require explicit `try_personal_message: false` or the debug CLI's `--no-message`.
- Unknown or contradictory profile identity aborts with `profile_not_ready` or `profile_identity_mismatch`. An ambiguous/wrong invitation recipient aborts with `modal_recipient_mismatch`; a first name alone without an exact target profile link is insufficient. Once Connect may have been dispatched, non-platform confirmation errors are logged with their original cause and held as `invite_not_confirmed`.
- Audience filtering parses only the owner's verified count phrases, wherever they appear on the page (followers often sit in the Activity header rather than the topcard). Missing required counts produce `audience_unavailable`; genuinely below-threshold counts produce `audience_filter`.
- Only explicitly marked **pre-send** `profile_not_ready` and `audience_unavailable` outcomes can be deferred: at most two retries, no earlier than 30 minutes and then six hours. Their `not_before` and `preflight_retries` fields survive restarts. A reason string alone never authorizes retry.
- Audience rejections, identity/modal mismatches, generic navigation/selector failures, exhausted preflight retries, and uncertain sends remain terminal. Notification engagement cannot resurrect terminal tasks or reset deferred retry budgets.
- Note and Send controls are scoped to the verified invitation dialog. A requested note must remain visible and its readback must match the generated note before Send; stale text is cleared only on the explicit no-note path.
- LinkedIn's Add a note transition may temporarily hide the dialog and remove recipient text. Readiness polls for up to three seconds, revalidating the recipient on every poll; foreign/ambiguous recipients abort immediately. Only the same connected dialog element can retain the recipient verified before that transition, and only for that attempt. Supported textarea/contenteditable editors use the same selector for readiness and same-element evidence, without treating generic chat editors as invitation proof. A fresh/replaced nameless editor cannot inherit that verification; conflicting names or profile links still abort.
- Before Connect, existing invitation dialogs abort even if their recipient matches. The same invitation-specific verifier is used before and after Connect; unrelated chat or embedded video-error dialogs do not qualify as invitation dialogs.
- Connect/More discovery shortlists labels in one browser read and uses structural locator hints to avoid repeatedly scanning every page control. Heuristic, cached, and LLM candidates must pass a non-dispatching one-second actionability trial so covered sticky duplicates do not hide an actionable main control. Hints never grant action authority: recipient identity, ownership, and menu provenance are revalidated after the trial's possible scrolling and again before the actual click. Real Connect/More clicks have a five-second timeout; failures log the underlying cause and never authorize another Connect.
- A potentially dispatched Connect/Send click is never blindly retried. Completion requires the verified target to show Pending/Connected; an unrelated success toast is not enough. If Connect directly submits before a requested note can be entered, the outcome is held as `invite_not_confirmed`, not reported as personalized success.
- `completed` means an invitation workflow was confirmed, not that the recipient later accepted. Acceptance is not tracked.
- These checks deliberately fail closed for unsupported layouts. Do not restore page-wide `.first` selectors or post-click content re-scraping, and never pass unverified LLM output or raw page text to note generation.

### Rate limits and spacing

The dispatcher preserves rolling caps while using quota-aware slots for invitations and randomized spacing for other actions.

- invites: 10 quota-consuming outcomes per rolling 24 hours; confirmed requests and unresolved `invite_not_confirmed` outcomes both reserve slots conservatively
- posts: 50 per rolling 24 hours
- autonomous feed comments: 12 per rolling 24 hours

Invitation eligibility is reconstructed from persisted quota events. When capacity is available, the worker can attempt an invite after the minimum gap of `0.7 × 24 hours / cap` (100 minutes 48 seconds at cap 10); it does not redistribute free slots against a moving oldest-event horizon. At a full quota it waits until the oldest relevant event exits the inclusive rolling window. An expiring event must release capacity, not move the deadline later. Cooldowns and profile-visit throttles still take precedence; no catch-up bursts or cap increase are allowed. Posts/comments retain `0.7x`–`1.3x` randomized spacing.

Additional cooldowns are applied for known LinkedIn skip reasons:

- invite weekly limit: 7 days
- invite withdrawal cooldown: 6 hours
- no safe commentable feed posts: 30 minutes
- autonomous comment hard failure: 30 minutes

## Persistence and Local State

- WebKit profile: `data/connection-machine-webkit`; the legacy `data/connection-machine-chrome` profile is not read, converted or deleted
- SQLite database: whatever `DATABASE_URL` points to
- autonomous feed-comment history: `data/feed_comment_history.json`

The first acquisition checks the public IP. Each acquisition checks authentication before any queued task is claimed; debug and auth-only commands still acquire immediately. Persistent login cookies and local storage belong to the WebKit profile; keep it private on the worker host.

Startup runs `init_db()` before browser activity. Existing task databases receive the `not_before` and `preflight_retries` columns plus three indexes for runnable order, earliest deferral and recent quota events. Index creation is idempotent but can hold a write lock on first upgrade: back up live state, allow disk space for the indexes, and stop the previous worker. Older workers do not honor deferred-retry deadlines. Local history files retain the same JSON schema; writes stream compact JSON to a sibling temporary file and atomically replace the previous snapshot.

### Migrating an existing worker

Previously Patchright held Chromium, a page and its driver open through every quota wait. The replacement keeps the Python dispatcher, database, task code, OpenRouter calls and metrics. WebKit supplies the layout, screenshots, clicks, editable controls and JavaScript that the handlers require. An HTTP client cannot run those flows unchanged; swapping in a Rust Chrome driver would still run Chrome.

1. Build the new image and keep the old image for rollback. Stop the old worker after its current action finishes. Back up the task database and mounted `data/` while stopped. Never run two consumers against the same queue or two browsers on the same profile.
2. On the worker host, with the same account configuration and data mount, run `docker compose run --rm --no-deps connection-machine python src/main.py --check-auth`. This opens the feed and logs in if needed; it does not initialize the database, start the dispatcher, queue invitations or post. WebKit needs its own first login. Do not rename the Chrome profile or export cookies. If the account requires a checkpoint, stop and resolve it with the account owner before enabling the worker.
3. Run the auth check a second time after the first process exits. Require an authenticated feed without a new login. Start one new worker with the existing volume. Monitor auth, `invite_not_confirmed`, failed tasks, polling and container memory through an actual workload before lowering any container limit.
4. To roll back, stop WebKit and restart the old image using the **current** queue, history, quota state and untouched Chrome profile. Do not restore an earlier database snapshot just to change engines: it can replay work completed since the backup.

### 100 MB acceptance (not met)

Do not put a 100 MB limit on the live worker: browser startup failed at that limit, even before an account or a real feed was involved. A process killed during a send may require manual reconciliation. No authenticated end-to-end success at this limit has been established.

```sh
docker build -t connection-machine:100mb-check .
python3 utils/accept_100mb.py > /tmp/connection-machine-acceptance.jsonl
```

The runner tests the packaged source in fresh Linux containers without a network, credentials or production volumes. The hard limit is **99,999,744 bytes** (100 decimal MB rounded down to a kernel page); `memory.swap.max=0`. Each record contains cgroup peak bytes, CPU microseconds, exit status and any OOM or timeout. A skip or missing measurement is not a pass. On the optimized Linux/arm64 image, 15 of 26 checks passed. All 11 browser fixtures reached the limit and failed: seven reported OOM, four timed out. Login persistence, invitation delivery, posting, commenting and real-page previews remain unverified under this cap.

### Local optimization measurements

Compared with `da3b5be` in separate offline Linux/arm64 containers, one CPU, no swap, the same 99,999,744-byte limit. CPU figures are process CPU time for the named work, excluding fixture setup; peaks include the container's charged file cache and are not a production sizing guarantee.

| Workload | Before | After |
|---|---:|---:|
| Cold idle worker with metrics: Python RSS, two polls under a durable cooldown | 64.82 MB | 54.10 MB |
| Same run, whole-container peak from startup to shutdown | 89.69 MB | 46.55 MB |
| 100 full-quota polls, 10,000 pending invites, metrics enabled | 1.565 CPU-s / 1,000 SQL statements | 0.528 CPU-s / 600 statements |
| Same polls, metrics disabled | 1.543 CPU-s / 1,000 statements | 0.226 CPU-s / 400 statements |
| 100 dashboard reads of unchanged 2,000-profile notification state | 0.330 CPU-s | 0.0041 CPU-s |
| Cleanup of 36,000 interrupted or obsolete fixture tasks | 0.433 CPU-s / 97.62 MB peak | 0.064 CPU-s / 56.63 MB peak |
| Minify a 1.60-million-character selector context to the existing 50,000-character budget | 0.106 CPU-s | 0.0050 CPU-s, identical output |
| Write/read notification state with 15,000 profiles | 93.65 MB peak / 6.61 MB file | 69.08 MB peak / 5.32 MB file |
| Import a 300,000-row synthetic CSV | OOM on 15 MB input | 10,000 selected and verified in order, 78.31 MB peak |

The importer also completed a **135.38 MB** CSV under the same cap, at the memory limit without an OOM kill. It uses temporary disk for exact first-occurrence deduplication. Selection still takes the top 10,000 *before* excluding invites already in the database. These measurements exercise the scheduler, import and serialization paths; none demonstrates that a real authenticated page fits.

### Browser alternatives measured

Candidate probes below used the same whole-container limit with no swap. A renderer-only failure rules out that tested configuration; a renderer-only success does not prove a worker with its scheduler, real login and live page fits. The fixtures differ by capability; the peaks are **not** a performance ranking.

| Local implementation path | Peak MB, decimal (rounded) | Observed gate |
|---|---:|---|
| Playwright + WebKit worker | 100.0 | OOM before the live auth preflight could submit a password |
| Native WPE MiniBrowser, no automation driver | 100.0 | Software rendering and JIT off; OOM before fixture JS proof |
| Native WebKitGTK + Xvfb/Cairo | 100.0 | Trusted click on a tiny fixture without the worker; separate resident scheduler killed in one run |
| WebKitGTK embedded via `ctypes` in the scheduler | 100.0 | No Node or MiniBrowser; OOM before fixture JS or click proof |
| Rust Servo 0.8.0, headless | 100.0 | Killed before producing the requested fixture screenshot |
| Firefox ESR 153.7, headless | 100.0 | No verified page before the 12-second deadline; also reported a container namespace warning |
| Chromium 156 headless shell, single process (comparison only) | 100.0 | Requested fixture, did not finish within 12 seconds |
| NetSurf GTK 3.11 | 74.1 | Requested fixture; no JS hit-test or module success signal before timeout |
| Lightpanda native PandaScript, no Playwright | 31.6 | Editor typing worked. The covered-click fixture used 13.3 MB but **dispatched** on the covered control; the screenshot missed its opaque overlay. Not safe for invites |
| HtmlUnit 5.8, constrained JVM | 69.0 | No working hit-test for the covered control |
| jsdom 31 | 100.0 | Zero target bounds and no hit-test |
| Happy DOM 21 | 56.2 | Zero target bounds and no hit-test |

A browserless worker would need an authorized interface for **all** of the actions and data above. LinkedIn’s [Invitations API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/communications/invitations) and [Profile API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api) are restricted. Ordinary member OAuth is not equivalent to an authorized source of prospects’ full profiles, the member’s home feed and personal notifications. A cookie is not an API grant.

## Environment Variables

The application loads `.env` automatically via `python-dotenv`. Copy `.env.example` to `.env` and adjust values.

Required for a normal worker run:

- `LINKEDIN_USERNAME`: LinkedIn login email or username
- `LINKEDIN_PASSWORD`: LinkedIn password
- `DATABASE_URL`: SQLAlchemy database URL. For local development use `sqlite:///data/connection_machine.db`

Optional but important:

- `OPENROUTER_API_KEY`: enables profile parsing (personalization and the audience filter), AI-generated invite notes, connect-action detection, and autonomous feed comments
- `LLM_MODEL`: OpenRouter model slug used by every LLM module. Defaults to `google/gemini-3.7-flash`
- `LLM_MODEL_CONNECTION_MESSAGE`, `LLM_MODEL_REFINE_TEXT`, `LLM_MODEL_FEED_COMMENT`, `LLM_MODEL_CONNECT_ACTION`, `LLM_MODEL_PROFILE_PARSE`: per-module overrides. Each falls back to `LLM_MODEL`, then to the built-in default
- `HEADLESS`: browser visibility toggle. Defaults to `true`
- `INVITE_MIN_FOLLOWERS`: minimum visible follower count; `0` disables this threshold
- `INVITE_REQUIRE_500_CONNECTIONS`: require a visible count of at least 500 connections when enabled
- `INVITE_MIN_VISIT_INTERVAL_SECONDS`: additional randomized throttle between profile visits, including skips; defaults to 90 seconds
- `TELEGRAM_NOTIFICATIONS_URL`: notification endpoint
- `TELEGRAM_CHAT_ID`: notification target
- `TELEGRAM_API_KEY`: bearer token for the notification endpoint

Behavior notes:

- default personalized invite tasks and the audience filter require `OPENROUTER_API_KEY`; without it they defer and then fail before Connect rather than sending without a note or unchecked counts. Explicit no-note tasks with the audience filter disabled can still use deterministic Connect discovery without the key.
- autonomous feed comments require `OPENROUTER_API_KEY`
- model selection is resolved per request in `src/llm.py:resolve_model`, so changing `LLM_MODEL*` only needs a worker restart
- `LLM_MODEL_CONNECT_ACTION` must name a vision-capable model; that module sends a screenshot
- notifications are silently skipped unless all three Telegram-related variables are set

## Local Development

```bash
# Install the locked Python client and its matching browser
uv sync --frozen
uv run playwright install webkit  # Linux: use install --with-deps webkit

# run the main worker
uv run python src/main.py

# Three independent previews against an already authenticated WebKit profile.
uv run python src/main.py --dry-run post
uv run python src/main.py --dry-run comment
uv run python src/main.py --dry-run notifications

# run one invite directly without using the DB queue
uv run python src/main.py --debug-invite "https://linkedin.com/in/profile-url"

# run one invite directly and skip the AI-generated note
uv run python src/main.py --debug-invite "https://linkedin.com/in/profile-url" --no-message

# generate one feed comment without submitting it
uv run python src/main.py --debug-feed-comment

# generate one feed comment and actually submit it
uv run python src/main.py --debug-feed-comment --submit-comment

# populate DB tasks from CSV
uv run python utils/populate_db.py
```

`--dry-run post` verifies a visible, editable post composer without typing into a possibly autosaved draft. `comment` selects a feed post and generates a comment through OpenRouter, without opening the editor, notifying Telegram or storing history. `notifications` discovers a qualifying reply candidate without opening the database or updating discovery state; no candidate is a skip, not confirmation. These commands require member navigation on the expected LinkedIn route and refuse an expired session rather than submitting credentials. Normal page views may still be visible to LinkedIn.

`--debug-invite URL` is **live**, not a preview: Connect can send immediately. Only use it with the designated recipient and verified account quota; it does not go through dispatcher scheduling. The notification debug command can enqueue tasks even without `--send-notification-invite`; use `--dry-run notifications` for a non-enqueuing check.

Run the isolated regression suite from the repository root:

```bash
DATABASE_URL=sqlite:///:memory: uv run python -m unittest discover -v
```

Browser regressions use intercepted synthetic pages and temporary profiles, never the live session. The suite blanks Telegram/OpenRouter credentials and blocks non-fixture browser traffic. Missing WebKit fails browser setup; tests do not download browsers or silently fall back to Chrome.

## Docker

The image contains the worker, import utility and WebKit, not local `.env`, prospect lists or browser profiles. To run `utils/populate_db.py` in Docker, mount the input CSV read-only at `/app/utils/people_all.csv`.

The container expects `.env` through `docker-compose.yaml` and mounts `./data` into `/app/data`.

```bash
docker-compose up -d
docker-compose logs -f connection-machine
```

## Agent Guidance

When changing this codebase:

- preserve persistent-session behavior unless the task explicitly requires auth changes
- keep automation conservative; the dispatcher intentionally spaces work out
- prefer documenting real behavior from code, not planned behavior
- do not reintroduce DB-backed feed comments unless that is the explicit task
- treat `data/` as runtime state, not source-controlled configuration
- if you add a new env var, update both `.env.example` and this file in the same change
- after completing the requested work and its verification, always create a local git commit covering the task's changes before the final handoff; exclude unrelated pre-existing changes, and do not push or deploy without explicit authorization

## Common Pitfalls

- `DATABASE_URL` must be set or imports from `src/db.py` will fail immediately
- after a Playwright upgrade, install its matching WebKit build or rebuild the container; there is no system-browser fallback
- LinkedIn auth can land on `/checkpoint`; that is treated as not authenticated
- autonomous comments are intentionally disabled when no OpenRouter key is present
