# CLAUDE.md — Missed Call Text-Back (MCTB)

This file gives Claude Code context for the project. Full specification: `spec.md` (source of truth for architecture, DB schema, and business rules). On conflict: spec.md > CLAUDE.md > code.

## What this project is

Multi-tenant SaaS, currently serving only the Polish market (see spec.md section 4.0 for the country-rules registry that makes adding a second market a config addition, not a rewrite): a client forwards missed calls (GSM codes set with their carrier) to their assigned virtual Twilio number; the system rejects the call (TwiML `<Reject/>`), identifies the client by the destination number (`To`), generates a reply via AI (Phase 1), and sends a text-back SMS to the caller in their local language.

Flow: `Twilio webhook → FastAPI (<1s, TwiML + enqueue only) → Celery worker (guards → AI → SMS) → Twilio Messages API`.

## Tooling & Testing Workflow

Follow this after every code change, without exception:

- Lint/type/test tooling lives in the project's `uv`-managed `.venv/` (not on PATH) — always invoke it via `.venv/bin/`, e.g. `./.venv/bin/ruff`, `./.venv/bin/mypy`, `./.venv/bin/pytest` (equivalently `uv run ruff`/`uv run mypy`/`uv run pytest`). Dev tools (ruff/mypy/pytest/fakeredis) are the `dev` extra — `uv sync --extra dev` installs them.
- After writing or modifying any code, run `./.venv/bin/ruff format .` and `./.venv/bin/ruff check . --fix` in the terminal to format the code and fix lint errors.
- Use `./.venv/bin/mypy app` to verify typing correctness.
- Every piece of core business logic (e.g. character sanitization, rate limits, deduplication) **must** have `pytest` tests. After writing tests, run them with `./.venv/bin/pytest`.
- **Write the test immediately after the function, not later.** As soon as a core function is implemented (e.g. `prepare_sms_body`, `normalize_e164`, `acquire_cooldown`), stop and write its test file before moving to the next piece of work — don't batch testing to the end of a session.
- Don't consider a task done until `ruff format`, `ruff check --fix`, `mypy`, and `pytest` all pass clean. Fix failures before moving on — don't leave them for a later pass.

```bash
# run after every change, in this order
./.venv/bin/ruff format .
./.venv/bin/ruff check . --fix
./.venv/bin/mypy app
./.venv/bin/pytest
```

## Commands

```bash
# dev environment
docker compose up -d db redis
uv sync --extra dev                        # dependencies, incl. ruff/mypy/pytest (pyproject.toml)

# running
uvicorn app.main:app --reload --port 8000  # API
celery -A app.workers.celery_app worker -l info
celery -A app.workers.celery_app beat -l info   # GDPR jobs / counters

# database
alembic upgrade head
alembic revision --autogenerate -m "description"

# tests and code quality — run before every commit (tools live in .venv/, not on PATH)
./.venv/bin/pytest                                     # full suite
./.venv/bin/pytest tests/test_sms_encoding.py -v       # GSM-7 golden tests (critical)
./.venv/bin/ruff check . && ./.venv/bin/ruff format .
./.venv/bin/mypy app

# local webhooks
ngrok http 8000                            # set PUBLIC_BASE_URL to the ngrok URL,
                                           # otherwise Twilio signature validation will reject requests
```

## Local dev — never spend real SMS budget

- **Unit tests and CI never touch the real Twilio API** — `twilio.messages.create` and the AI client are always mocked (see Testing section). This is non-negotiable, not a convenience.
- **For manual/local smoke-testing of the webhook → SMS flow**, use Twilio's separate **test credentials** (a Test Account SID + Test Auth Token pair, found in the Twilio Console next to the live ones) together with Twilio's documented test/"magic" phone numbers, instead of a real client's Twilio number or a real PL mobile number. Test-credential requests never leave Twilio's system and never send a real SMS or incur cost.
- **Never use a production `TWILIO_AUTH_TOKEN` locally** to "just try it once" — `.env` (local) holds test credentials only; production secrets live only in the deployment's secret manager.
- If a test/magic number's exact behavior isn't obvious from spec.md, check Twilio's current docs rather than guessing — this detail can change and guessing wrong here risks a real charge.

## Structure (where to change what)

```
app/api/webhooks.py        # Twilio endpoints — ONLY signature validation, persist, enqueue, TwiML
app/workers/tasks.py       # process_missed_call — orchestration/guard chain only; delegates the actual send to sms_sender.py
app/services/guards.py     # cooldown, daily limits, opt-out, loop detection
app/services/tenant_resolver.py  # To → twilio_numbers → client + active prompt (60s cache)
app/services/ai_client.py  # Phase 1 adapter: httpx, 8s timeout, circuit breaker, fallback
app/services/sms_sender.py # Twilio Messages API call + status_callback wiring (spec.md 6.2) —
                            # tasks.py must call into this, not `twilio.messages.create` directly;
                            # the inline call in spec.md 3.4 is illustrative pseudocode, not a file placement
app/core/countries.py      # CountryRules registry (spec.md 4.0) — PL is the only entry today;
                            # add a country here, not by inlining new literals in phone.py/guards.py
app/core/phone.py          # normalize_e164, is_anonymous, is_mobile_number(phone_e164, country_code)
app/core/sms_encoding.py   # GSM7_SAFE, prepare_sms_body(..., country_code=...) — translit map comes
                            # from countries.py, not a hardcoded PL_TRANSLIT constant
app/models/                # SQLAlchemy — schema matching spec.md section 5
app/db.py                  # SQLAlchemy engine/session factory, shared by API + Celery + Alembic
```

## Invariants (don't break these without updating spec.md)

1. **The webhook handler never calls AI or the Twilio Messages API.** Budget < 1s: signature → `INSERT ... ON CONFLICT (call_sid) DO NOTHING` → enqueue → TwiML. Everything else happens in the worker.
2. **Every phone number in the system is in E.164** and passes through `normalize_e164()` at the entry point. No raw strings from the webhook go into the DB or into Twilio calls.
3. **Guard order in `process_missed_call` is significant** (cheapest first): tenant → anonymous/invalid → loop → opt-out → non-mobile → cooldown → daily limit. Wire new guards into this sequence and add a new status to the `call_events.status` CHECK constraint.
4. **AI failure never blocks the SMS.** Timeout/error → `ai_prompts.fallback_message`, `is_fallback=true`. Zero retries to AI within a single event.
5. **Transliteration to GSM-7 by default** (`allow_diacritics=false`). Any change to `sms_encoding.py` requires passing the golden tests — one leaked diacritic means 2–3x the SMS cost.
6. **Idempotency:** unique constraint on `call_events.call_sid`; before sending, the worker checks whether the event already has an `sms_messages` row (a retry must never duplicate an SMS).
7. **Cooldown via Redis `SET NX`** (atomic), not a DB `SELECT` — two calls in the same second is a real case.
8. **GDPR:** never log full phone numbers in application logs (structlog masks to `+4850***4567`); don't add personal-data fields without retention/anonymization; data stays in the EU region.
9. **SMS content:** no links, no marketing content, no personal data about the caller — these bans live in the system prompts; don't remove them "to save characters."
10. **Migrations are reviewed, never trusted blindly.** After `alembic revision --autogenerate`, read the generated diff line by line before running `alembic upgrade head` — autogenerate can silently drop a CHECK constraint, reorder an unsafe operation, or miss an index. Never hand-edit a migration that has already been applied; write a new one instead.
11. **Secrets never appear in output.** Don't `cat`, `print`, log, or otherwise echo the contents of `.env` or any variable holding `TWILIO_AUTH_TOKEN`, `DATABASE_URL` credentials, or `SENTRY_DSN` — including "for debugging." To check a var is set, check its presence or length, never its value.

## Code conventions

- **All code and comments are written in English** — identifiers, docstrings, commit messages, and code comments. This applies regardless of the language a conversation with Claude Code happens in.
- Python 3.12, full type hints, `mypy --strict` on `app/core` and `app/services`.
- Async in the API layer (FastAPI + httpx); Celery tasks are synchronous (separate SQLAlchemy session per task).
- Configuration only through `app/config.py` (Pydantic Settings); no `os.environ` scattered through the code.
- Event statuses: the Python enum must match the DB CHECK constraint 1:1 — changing it requires an Alembic migration.
- Logs: structlog JSON, always with `call_sid` and `client_id` as context fields.

## Modularity & File Size

- **No file should exceed ~300 lines.** If a file you're editing is approaching that (especially `app/workers/tasks.py` or `app/api/webhooks.py`), stop and propose a split before adding more code — don't let it grow past the point where earlier parts of it fall out of context.
- Stick strictly to the folder structure in the section above — one concern per file (webhooks ≠ business logic ≠ DB access ≠ AI calls). If a task doesn't have an obvious home in the existing structure, ask before inventing a new top-level module.
- When a file must grow, prefer extracting a new module over appending — e.g. split a growing `guards.py` into `guards/cooldown.py`, `guards/loop_detection.py`, etc.

## Git Workflow

- **Working 100% locally for now — do not run any `git` commands** (`add`, `commit`, `push`, `restore`, etc.) unless explicitly asked to in a given message. No auto-commit after tasks.
- Commits/history will be handled manually, in one pass, at the end — don't treat "not committed yet" as a task being incomplete.
- The quality gate is unchanged and still applies before calling a task "done": `ruff format`, `ruff check --fix`, `mypy`, and `pytest` must all pass clean.
- If a change is destructive or hard to reverse without git history (e.g. a large rewrite/deletion), flag it and ask before proceeding, since there's no commit safety net to fall back on right now.

```bash
# after a task is verified working — no git step
./.venv/bin/ruff format . && ./.venv/bin/ruff check . --fix && ./.venv/bin/mypy app && ./.venv/bin/pytest
```

## Testing

- `tests/test_sms_encoding.py` — golden tests: for a list of Polish sentences, assert no characters outside `GSM7_SAFE` and correct segment count. Don't mock — it's a pure function.
- `tests/test_webhooks.py` — generate Twilio signatures via `twilio.request_validator.RequestValidator` with a test token; also test rejection of a bad signature (403) and a duplicate `CallSid` (no second job enqueued).
- `tests/test_guards.py` — use fakeredis for cooldown/limits; cases: loop (From = a Twilio number), anonymous (`+266696687`), opt-out, second call within the 4h window.
- Twilio and AI are always mocked in unit tests; no network calls in CI.
- `.github/workflows/ci.yml` runs the exact same `ruff format --check` / `ruff check` / `mypy` / `pytest` sequence as the local workflow above — if you change the local sequence, update the workflow file in the same change so they don't drift apart.

## Known pitfalls in this project

- Twilio signature validation computes the HMAC over the **public** URL — behind a proxy/ngrok, use `PUBLIC_BASE_URL`, not `request.url`.
- `ForwardedFrom` is sometimes empty (depends on the carrier) — never use it for routing, only `To`.
- Twilio retries webhooks on anything other than 2xx — every error path in the handler must still return valid TwiML, or a deliberate 4xx.
- `Reject` must be the only verb in the TwiML — nothing after it executes.
- Redis TTL for daily limits is counted to midnight **Europe/Warsaw**, not UTC.