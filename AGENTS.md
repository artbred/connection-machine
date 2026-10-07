# AGENTS.md

This file gives coding agents working in this repository the minimum context they need to make safe changes without reverse-engineering the whole project first.

## Project Overview

Connection Machine is a LinkedIn automation worker built around a persistent browser session, a SQLite-backed task queue, and OpenRouter-powered text generation.

Today the codebase supports three practical automation flows:

- Sending LinkedIn connection requests from queued database tasks
- Creating LinkedIn posts from queued database tasks
- Generating feed comments autonomously when the account is idle and an OpenRouter key is configured

The browser layer is built on Patchright plus Playwright, with human-like interaction helpers to reduce obvious automation patterns.

## High-Level Runtime

```text
src/main.py
  -> load .env
  -> launch persistent Chromium context
  -> restore LinkedIn session or log in
  -> initialize database
  -> start TaskDispatcher loop

TaskDispatcher
  -> polls pending DB tasks
  -> enforces spacing and per-day limits
  -> runs matching task handler
  -> records success / failure / cooldown state
  -> opportunistically runs autonomous feed comments when no DB task is runnable
```

## Main Components

- `src/main.py`: entry point, browser startup, LinkedIn auth recovery, debug CLI modes, graceful shutdown
- `src/dispatcher.py`: polling loop, task selection, spacing windows, zombie cleanup, autonomous comment scheduling
- `src/db.py`: SQLAlchemy engine, `linkedin_tasks` table, enums, DB initialization
- `src/invite_schedule.py`: deterministic rolling-quota slots and minimum invitation spacing
- `src/tasks/invite.py`: profile visit, connect-button discovery, required-by-default AI-generated note, invitation confirmation
- `src/linkedin_profile.py`: canonical target identity, shared topcard ownership, allowlisted profile sections, bounded readiness checks
- `src/invite_modal.py`: recipient-verified invitation dialogs and scoped note/send controls
- `src/connect_heuristics.py`, `src/connection_state.py`: identity-bound Connect/More actions, cache hints, and target connection-state detection
- `src/tasks/post.py`: opens the supplied LinkedIn composer URL and submits a post
- `src/tasks/comment.py`: scans the feed for safe posts, asks the LLM for a comment, optionally publishes it, and stores local comment history
- `src/llm.py`: OpenRouter calls for connection notes, feed comments, and connect-action detection
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
- Interrupted `processing` invitations are held as `failed` / `invite_not_confirmed` for reconciliation, never blindly replayed; other stuck task types are reset to `pending`
- stale pending `create_post` tasks older than one hour are deleted
- legacy DB-backed feed-comment tasks are deleted
- Runnable invitations take priority over other queued task types and notification scans
- New notification scans/comments do not start within 15 minutes of a pending invitation becoming eligible; already-running synchronous actions are not forcibly interrupted
- Notification discovery runs in available idle windows, with its existing 30-minute minimum interval. With recent invite history and a backlog, it runs at most once per quota event; it never runs during a durable invite cooldown or exhausted invite quota.

### Invitation identity and personalization

- Every invite requires the queued URL, loaded profile, Connect target, and invitation dialog recipient to agree. Profile URLs are canonicalized; a canonical page link alone is not enough to prove the visible topcard belongs to the target.
- Personalization uses only the verified prospect's headline, About, and Experience sections. Activity, reposts, recommendations, and unbounded/ambiguous modules are excluded rather than sent to the LLM.
- Both classic profile cards and SDUI's keyed cards inside a `LazyColumn` are supported. SDUI About/Experience card keys must match the verified topcard owner, including `ExperienceTopLevelSection`. Owned text traversal supports boxless `display: contents` wrappers without treating hidden content, unrelated controls, or foreign modules as profile prose.
- Readiness waits up to eight seconds for substantive owned content and 600 ms of stable identity/fields. At least one substantive About/Experience section is required; absent sections are optional, but discovered empty/loading sections prevent early personalization. When personalization is requested (the default), headline-only or insufficient content defers/skips the task before Connect with `profile_not_ready`; it never falls back to a note-less invitation or whole-page text.
- A nonempty note of at most 200 characters must be generated before Connect. Missing/invalid generation aborts with `llm_invalid_response`; there is no silent no-note fallback. The same preflight-generated note is used throughout the attempt, without regeneration after any potentially dispatched click. Note-less invitations require explicit `try_personal_message: false` or the debug CLI's `--no-message`.
- Unknown or contradictory profile identity aborts with `profile_not_ready` or `profile_identity_mismatch`. An ambiguous/wrong invitation recipient aborts with `modal_recipient_mismatch`; a first name alone without an exact target profile link is insufficient. Once Connect may have been dispatched, non-platform confirmation errors are logged with their original cause and held as `invite_not_confirmed`.
- Audience filtering reads only the verified topcard. Required counts that remain unreadable produce `audience_unavailable`; genuinely below-threshold counts produce `audience_filter`.
- Only explicitly marked **pre-send** `profile_not_ready` and `audience_unavailable` outcomes can be deferred: at most two retries, no earlier than 30 minutes and then six hours. Their `not_before` and `preflight_retries` fields survive restarts. A reason string alone never authorizes retry.
- Audience rejections, identity/modal mismatches, generic navigation/selector failures, exhausted preflight retries, and uncertain sends remain terminal. Notification engagement cannot resurrect terminal tasks or reset deferred retry budgets.
- Note and Send controls are scoped to the verified invitation dialog. A requested note must remain visible and its readback must match the generated note before Send; stale text is cleared only on the explicit no-note path.
- Before Connect, existing invitation dialogs abort even if their recipient matches. The same invitation-specific verifier is used before and after Connect; unrelated chat or embedded video-error dialogs do not qualify as invitation dialogs.
- Connect/More discovery shortlists labels in one browser read and uses structural locator hints to avoid repeatedly scanning every page control. Hints never grant action authority: recipient identity, ownership, and menu provenance are revalidated before each click, including after scrolling or DOM replacement.
- A potentially dispatched Connect/Send click is never blindly retried. Completion requires the verified target to show Pending/Connected; an unrelated success toast is not enough. If Connect directly submits before a requested note can be entered, the outcome is held as `invite_not_confirmed`, not reported as personalized success.
- `completed` means an invitation workflow was confirmed, not that the recipient later accepted. Acceptance is not tracked.
- These checks deliberately fail closed for unsupported layouts. Do not restore page-wide `.first` selectors, whole-`main` text extraction, or post-click content re-scraping as fallbacks.

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

- browser session data: `data/connection-machine-chrome`
- SQLite database: whatever `DATABASE_URL` points to
- autonomous feed-comment history: `data/feed_comment_history.json`

The browser runs in a persistent context, so successful login state is meant to survive restarts.

Startup runs `init_db()` before browser activity. Existing task databases receive additive, idempotent `not_before` and `preflight_retries` columns without replacing task rows. Back up live state before rollout. Older workers can read the expanded schema but do not honor deferred-retry deadlines, so rollback requires attention to pending deferred tasks.

## Environment Variables

The application loads `.env` automatically via `python-dotenv`. Copy `.env.example` to `.env` and adjust values.

Required for a normal worker run:

- `LINKEDIN_USERNAME`: LinkedIn login email or username
- `LINKEDIN_PASSWORD`: LinkedIn password
- `DATABASE_URL`: SQLAlchemy database URL. For local development use `sqlite:///data/connection_machine.db`

Optional but important:

- `OPENROUTER_API_KEY`: enables AI-generated invite notes, connect-action detection, and autonomous feed comments
- `LLM_MODEL`: OpenRouter model slug used by every LLM module. Defaults to `google/gemini-3.7-flash`
- `LLM_MODEL_CONNECTION_MESSAGE`, `LLM_MODEL_REFINE_TEXT`, `LLM_MODEL_FEED_COMMENT`, `LLM_MODEL_CONNECT_ACTION`: per-module overrides. Each falls back to `LLM_MODEL`, then to the built-in default
- `HEADLESS`: browser visibility toggle. Defaults to `true`
- `INVITE_MIN_FOLLOWERS`: minimum visible follower count; `0` disables this threshold
- `INVITE_REQUIRE_500_CONNECTIONS`: require a visible count of at least 500 connections when enabled
- `INVITE_MIN_VISIT_INTERVAL_SECONDS`: additional randomized throttle between profile visits, including skips; defaults to 90 seconds
- `TELEGRAM_NOTIFICATIONS_URL`: notification endpoint
- `TELEGRAM_CHAT_ID`: notification target
- `TELEGRAM_API_KEY`: bearer token for the notification endpoint

Behavior notes:

- default personalized invite tasks require `OPENROUTER_API_KEY`; without it they fail before Connect rather than sending without a note. Explicit no-note tasks can still use deterministic Connect discovery without the key.
- autonomous feed comments require `OPENROUTER_API_KEY`
- model selection is resolved per request in `src/llm.py:resolve_model`, so changing `LLM_MODEL*` only needs a worker restart
- `LLM_MODEL_CONNECT_ACTION` must name a vision-capable model; that module sends a screenshot
- notifications are silently skipped unless all three Telegram-related variables are set

## Local Development

```bash
# install dependencies
uv sync

# run the main worker
uv run python src/main.py

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

Run the isolated regression suite from the repository root:

```bash
DATABASE_URL=sqlite:///:memory: uv run python -m unittest discover -v
```

Browser regressions use intercepted synthetic pages in fresh contexts, never the persistent LinkedIn session. The suite blanks Telegram/OpenRouter credentials and blocks non-fixture browser traffic. It uses installed Chromium or local Chrome without downloading a browser; missing browser support is reported as skipped coverage, not a verified browser pass.

## Docker

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
- a missing browser binary may cause Patchright to fall back to local Chrome
- LinkedIn auth can land on `/checkpoint`; that is treated as not authenticated
- autonomous comments are intentionally disabled when no OpenRouter key is present
