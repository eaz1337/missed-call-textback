# Technical Specification — MCTB-RAG SaaS

| Field | Value |
|---|---|
| Version | 1.1 |
| Status | For implementation (Phase 1 = MVP, Phase 2 = RAG — see section 9) |
| Target market | Poland (+48) — first entry in a country registry (section 4.0), not a hardcoded assumption |
| Dependencies | Twilio (Voice + SMS), Phase 1 AI module, Phase 2 RAG module (section 9) |

**Changelog v1.0 → v1.1:** added enterprise-readiness hooks that cost nothing to build for a small-business client but avoid a rewrite later (`access_group_id` placeholder, auth as a swappable dependency, audit log as an optional hook, RBAC-ready RAG schema). Added section 9, the Phase 2 RAG plan (next implementation step after MVP).

---

## 1. System purpose

The system automatically replies via SMS to missed incoming calls for clients (businesses) using the service. The client sets up **conditional call forwarding** with their GSM carrier (no answer / busy / out of range) to their assigned **virtual Polish Twilio number**. Twilio rejects the forwarded call and notifies the server via webhook; the server identifies the client by the destination number, generates content via AI (Phase 1), and sends a text-back SMS to the caller in Polish, acting as that client's assistant.

Key properties: the system is **multi-tenant** (a single deployment serves many clients), the response to the Twilio webhook is **synchronously immediate** (< 1s, TwiML only), and all business logic (AI + SMS) executes **asynchronously** in a task queue.

---

## 2. System architecture (SaaS, multi-tenant)

### 2.1 Component diagram

```
 Caller                    Client's phone               Twilio Cloud
 (+48 xxx xxx xxx)         (iOS/Android)                (virtual +48 number)
      │                        │                            │
      │ 1. call                │                            │
      ├───────────────────────►│                            │
      │                        │ 2. no answer               │
      │                        │    → GSM forwarding         │
      │                        ├───────────────────────────►│
      │                        │                            │ 3. Webhook POST /voice
      │                        │                            ▼
      │                 ┌─────────────────────────────────────────────┐
      │                 │  API Gateway (FastAPI)                      │
      │                 │  • X-Twilio-Signature validation             │
      │                 │  • TwiML <Reject/> response (< 1 s)         │
      │                 │  • enqueue: process_missed_call(CallSid)    │
      │                 └───────────────┬─────────────────────────────┘
      │                                 │ 4. job → Redis
      │                                 ▼
      │                 ┌─────────────────────────────────────────────┐
      │                 │  Worker (Celery)                            │
      │                 │  • Twilio number → client (tenant) mapping  │
      │                 │  • guards: dedup / blacklist / limits       │
      │                 │  • AI (Phase 1) → SMS content (8s timeout)  │
      │                 │  • RAG lookup (Phase 2, section 9) — optional│
      │                 │  • PL normalization (GSM-7 / transliteration)│
      │                 │  • Twilio Messages API → SMS                │
      │                 └───────┬─────────────────────┬───────────────┘
      │                         │                     │
      │                  ┌──────▼──────┐       ┌──────▼──────┐
      │                  │ PostgreSQL  │       │ AI Service  │
      │                  │ (tenant DB) │       │ (Phase 1/2) │
      │                  └─────────────┘       └─────────────┘
      │ 5. Text-back SMS (from the client's assistant)
      ◄─────────────────────────────────────────── Twilio SMS
```

### 2.2 Multi-tenancy model

Model used: **shared database, shared schema** with a `client_id` (UUID) column in every tenant-scoped table. At this scale (hundreds–thousands of clients, webhook-driven traffic) this is the simplest and cheapest option; per-schema/per-DB isolation adds no value for this data profile.

**The call's destination number is the key for tenant routing.** Each client has at least one virtual Twilio number assigned (1:N relationship). The Voice webhook contains a `To` parameter — the Twilio number the carrier forwarded the call to. The server performs a lookup:

```
To (+48732xxxxxx) → twilio_numbers.phone_e164 → twilio_numbers.client_id → clients + ai_prompts
```

This means a single webhook endpoint (`/webhooks/twilio/voice`) handles **all** numbers in the system — no per-client URL configuration is needed. AI behavior (system prompt, tone, company data, fallback) is loaded dynamically from the `ai_prompts` table by `client_id`.

### 2.3 Client provisioning (onboarding)

1. Create a `clients` record (company data, owner's mobile number in E.164).
2. Purchase a Polish **mobile** number in Twilio (`AvailablePhoneNumbers/PL/Mobile`) — the number must be mobile so it can send SMS and be credible to the recipient (able to call back). Operational note: Polish numbers in Twilio require an approved **Regulatory Bundle** (company data + address in PL/EU) — keep one bundle at the platform level.
3. Configure the number: `VoiceUrl = https://api.<domain>/webhooks/twilio/voice`, `SmsUrl = https://api.<domain>/webhooks/twilio/inbound-sms`, POST method.
4. Write to `twilio_numbers` (number → client mapping) and create a default `ai_prompts` record.
5. Generate the GSM code instructions (section 3.1) for the client, with their Twilio number substituted in.

---

## 3. Data Flow

### 3.1 Client-side call forwarding configuration (GSM/MMI codes)

The client dials these on their phone (works with every Polish carrier — Orange, Play, Plus, T-Mobile — these are standard GSM codes):

| Scenario | Activation | Deactivation |
|---|---|---|
| No answer (after ~25s) | `**61*+48732XXXXXX**25#` | `##61#` |
| Line busy | `**67*+48732XXXXXX#` | `##67#` |
| Out of range / off | `**62*+48732XXXXXX#` | `##62#` |
| All conditional at once | `**004*+48732XXXXXX#` | `##004#` |

Product recommendation: instruct the client to set `**004*...#` (all conditions). The server-side system behaves identically regardless of the forwarding reason.

### 3.2 Voice webhook — incoming call

Twilio sends a `POST` to `VoiceUrl` with `Content-Type: application/x-www-form-urlencoded`. Example payload (decoded):

```json
{
  "CallSid": "CA7a3f1e92b4c8d5a6f0e1b2c3d4e5f6a7",
  "AccountSid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "From": "+48501234567",
  "To": "+48732000111",
  "CallStatus": "ringing",
  "Direction": "inbound",
  "ForwardedFrom": "+48601998877",
  "CallerName": "",
  "FromCountry": "PL",
  "ToCountry": "PL",
  "ApiVersion": "2010-04-01"
}
```

Meaning of the critical fields:

| Field | Role in the system |
|---|---|
| `To` | **Tenant routing key** — Twilio number, lookup in `twilio_numbers` → `client_id`. |
| `From` | Caller's number — recipient of the text-back SMS. May be `anonymous` / `+266696687` (section 7.4). |
| `CallSid` | Unique call identifier — **idempotency key** (unique on `call_events.call_sid`). |
| `ForwardedFrom` | The client's number the call was forwarded from (SIP Diversion header). **Optional field** — not every carrier passes it. When present, it serves as a cross-check on the mapping and for loop detection; **never** as the basis for routing. |

### 3.3 Server response — TwiML

The server responds immediately (without waiting for AI):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Reject reason="busy"/>
</Response>
```

Rationale for `<Reject>` instead of `<Hangup/>`: Twilio rejects the call **before it's answered** — an incoming call that's never answered isn't billed, and the caller hears a busy tone (a natural ending for an unanswered call). `<Hangup/>` answers the call first (cost + the caller hears silence).

The webhook handler does only: (1) signature validation, (2) persisting the raw event + enqueueing the job, (3) returning TwiML. Time budget: **< 1s** (Twilio's timeout is 15s, but every added delay lengthens the tone the caller hears).

### 3.4 Asynchronous pipeline — `process_missed_call(call_sid)`

Celery task pseudocode (guard order matters — cheapest to most expensive):

```python
@celery_app.task(bind=True, max_retries=3, retry_backoff=True)
def process_missed_call(self, call_sid: str):
    event = db.get_call_event(call_sid)  # persisted by the webhook

    # --- GUARD 1: tenant routing ---
    number = db.get_twilio_number(event.to_number)
    if number is None or not number.is_active:
        event.mark("no_tenant")
        return  # orphan number → alert
    client = db.get_client(number.client_id)
    if client.status != "active":
        event.mark("client_suspended")
        return

    # --- GUARD 2: caller filter ---
    if is_anonymous(event.caller):  # section 7.4
        event.mark("anonymous")
        return
    caller = normalize_e164(event.caller)  # section 4.1
    if caller is None:
        event.mark("invalid_number")
        return
    if db.is_system_number(caller) or caller == client.owner_phone_e164:
        event.mark("loop_detected")
        return  # section 7.2
    if db.is_opted_out(client.id, caller):
        event.mark("opted_out")
        return
    if not is_mobile_number(caller, client.country_code):  # section 7.5
        event.mark("non_mobile")
        return

    # --- GUARD 3: dedup + limits (Redis) ---
    if not acquire_cooldown(client.id, caller, ttl=4 * 3600):  # section 7.3
        event.mark("deduplicated")
        return
    if not check_daily_limit(client.id):
        event.mark("limit_exceeded")
        notify_ops(client)
        return

    # --- Content generation ---
    prompt = db.get_active_prompt(client.id)
    try:
        # Phase 2 (section 9): ai_client.generate() internally calls the
        # retriever first when client.rag_enabled is true, and folds the
        # retrieved chunks into the prompt context. Phase 1 clients (no RAG)
        # skip straight to generation — same call signature either way, so
        # this task body doesn't change when Phase 2 ships.
        body = ai_client.generate(prompt, context=event, timeout=8.0)
    except (TimeoutError, AIServiceError):
        body = prompt.fallback_message  # section 7.6

    body = prepare_sms_body(
        body,
        allow_diacritics=prompt.allow_diacritics,
        max_segments=prompt.max_sms_segments,
    )  # section 4.2

    # --- Sending ---
    msg = twilio.messages.create(
        from_=number.phone_e164,
        to=caller,
        body=body,
        status_callback="https://api.<domain>/webhooks/twilio/sms-status",
    )
    db.save_sms(event, msg.sid, body)
    audit_log.record(client.id, "sms_sent", call_sid=call_sid, sms_sid=msg.sid)  # section 7.7
    event.mark("sms_queued")
```

### 3.5 SMS status webhook

Twilio reports message lifecycle events on `status_callback` (`queued → sent → delivered | failed | undelivered`):

```json
{
  "MessageSid": "SM9f2c1a7b6d5e4f3a2b1c0d9e8f7a6b5c",
  "MessageStatus": "delivered",
  "To": "+48501234567",
  "From": "+48732000111",
  "ErrorCode": ""
}
```

The handler updates `sms_messages.status` by `MessageSid`. Error codes that need handling: `21610` (recipient sent STOP — add to `opt_outs`), `21614` / `30006` (number doesn't accept SMS, e.g. a landline — mark the number as non-mobile in cache), `30003` (unreachable — no retry, log).

### 3.6 Inbound SMS webhook (`/webhooks/twilio/inbound-sms`)

Recipients can reply to the Twilio number. Minimum MVP scope: handling opt-outs — if the content (after trim/upper) is `STOP`, `KONIEC`, `NIE`, or `REZYGNUJE/REZYGNUJĘ` (Polish for "stop"/"end"/"no"/"I opt out"), append `(client_id, number)` to `opt_outs` and reply with confirmation TwiML. Other replies: log entry + (optional, post-MVP) notify the client by email/push.

---
## 4. Specifics of the Polish market

### 4.0 Multi-country extensibility

Poland is the only market implemented today, but the code doesn't hardcode
`+48` / Polish rules outside of one place: `app/core/countries.py` defines a
`CountryRules` dataclass (mobile-number prefixes, the `phonenumbers` region
hint, the diacritic transliteration map, inbound SMS opt-out keywords) and a
`COUNTRY_REGISTRY: dict[str, CountryRules]` keyed by ISO 3166-1 alpha-2 code.
`clients.country_code` (default `'PL'`) selects which entry applies to a
given client. `app/core/phone.py` (`is_mobile_number`) and, from Week 2,
`app/core/sms_encoding.py` (`prepare_sms_body`) and `app/services/guards.py`
read from this registry instead of inlining PL-specific literals.

Consequence for the DB layer: `twilio_numbers.phone_e164` uses the generic
E.164 CHECK (`^\+[1-9][0-9]{6,14}$`), the same one `clients.owner_phone_e164`
already used — not a PL-only `^\+48[0-9]{9}$` pattern — so a future
country's numbers aren't rejected at the schema level.

Adding a second country later means: one new `CountryRules` entry in the
registry, a Regulatory Bundle + number pool purchase in Twilio for that
country (section 2.3), and QA on that country's opt-out keywords/mobile
prefixes — not a rewrite of guards.py, phone.py, or the schema.

### 4.1 Enforcing the E.164 format (+48)

Every phone number in the system (callers, Twilio numbers, client numbers) is stored **exclusively** in E.164 format. Normalization happens at every entry point (webhook, client panel, import):

```python
import phonenumbers


def normalize_e164(raw: str, default_region: str = "PL") -> str | None:
    """Returns the number in E.164 (+48XXXXXXXXX) or None if the number is invalid.
    Accepts variants: '501234567', '48501234567', '0048501234567',
    '+48 501 234 567', '501-234-567'."""
    if not raw or raw.lower() in {"anonymous", "unknown", "restricted"}:
        return None
    try:
        parsed = phonenumbers.parse(raw.strip(), default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
```

Hard rules: (1) validate with `phonenumbers.is_valid_number` — not just syntactic correctness; (2) a DB constraint `CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$')`; (3) Twilio numbers are purchased exclusively from the PL pool, so `To` always arrives as `+48...` — normalize anyway.

### 4.2 SMS length optimization — Polish diacritics

**Problem:** an SMS in GSM-7 encoding fits 160 characters per segment. No Polish diacritic character (`ą ć ę ł ń ó ś ź ż` and their uppercase equivalents) **belongs to the GSM-7 alphabet** — a single such character in the body switches the entire message to UCS-2, cutting the limit to **70 characters** per segment. For a typical assistant reply (~140 characters), that means 2–3 segments instead of 1 — i.e. 2–3x higher cost.

| Encoding | 1 segment | Segment in a concatenated message |
|---|---|---|
| GSM-7 (no Polish characters) | 160 chars | 153 chars |
| UCS-2 (with Polish characters) | 70 chars | 67 chars |

**Default strategy: transliteration** (standard practice in Polish business SMS traffic, fully culturally acceptable — "Dziekujemy za telefon" surprises no one):

```python
PL_TRANSLIT = str.maketrans(
    {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z",
        "Ą": "A",
        "Ć": "C",
        "Ę": "E",
        "Ł": "L",
        "Ń": "N",
        "Ó": "O",
        "Ś": "S",
        "Ź": "Z",
        "Ż": "Z",
    }
)

# Subset of GSM-7 sufficient for validation after transliteration
GSM7_SAFE = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def prepare_sms_body(text: str, *, allow_diacritics: bool, max_segments: int) -> str:
    text = " ".join(text.split())  # whitespace normalization
    if not allow_diacritics:
        text = text.translate(PL_TRANSLIT)
        text = "".join(ch if ch in GSM7_SAFE else "?" for ch in text)  # hard GSM-7 closure
        limit = 160 if max_segments == 1 else max_segments * 153
    else:
        limit = 70 if max_segments == 1 else max_segments * 67
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text
```

System rules: (1) an `ai_prompts.allow_diacritics` flag per client, **defaulting to `false`**; (2) the AI system prompt embeds a "max 150 characters, no Polish characters" constraint — code-level transliteration is a safety net, not the only mechanism; (3) **disable** Twilio Smart Encoding on the Messaging Service — we normalize ourselves, deterministically; (4) log `sms_messages.encoding` and `segments` (computed before sending) to monitor cost per client.

Implementation note (Week 2): `PL_TRANSLIT` above is illustrative — in code, `prepare_sms_body` takes a `country_code` and looks up `CountryRules.translit_map` from `app/core/countries.py` (section 4.0) instead of importing a PL-only constant, so a future country supplies its own translit map without touching this function.

### 4.3 GDPR — call logs and caller data

A caller's phone number is personal data. Legal construction of the service:

**Roles.** The client (business) is the **data controller** for the data of people calling their number; the MCTB-RAG platform operator is the **data processor** (GDPR Art. 28). A **Data Processing Agreement (DPA)** is required as an integral part of the SaaS terms. Twilio acts as a sub-processor — list it in the DPA's sub-processor list.

**Legal basis for sending the SMS.** Legitimate interest of the controller (GDPR Art. 6(1)(f)): responding to contact initiated by the caller themself. Boundary condition: the SMS content must be **informational** (confirmation of the missed call, callback information, business hours). Marketing content would require separate consent (Electronic Communications Law — prohibition on unsolicited commercial information) — **the AI system prompt must explicitly prohibit promotional content**.

**Data minimization.** The call is rejected before being answered — the system **does not record and technically cannot record conversations**. Only metadata is stored: numbers, timestamps, statuses, the sent SMS content.

**Retention and anonymization.** Parameter `clients.log_retention_days` (default **90 days**). Nightly job (Celery beat):

```sql
UPDATE call_events
SET caller_e164 = NULL,
    caller_hash = encode(sha256((caller_e164 || :salt_per_client)::bytea), 'hex'),
    anonymized_at = now()
WHERE received_at < now() - (SELECT log_retention_days FROM clients c WHERE c.id = client_id) * interval '1 day'
  AND anonymized_at IS NULL;
```

The hash (SHA-256 with a per-client salt) preserves statistics (unique callers, repeat rate) without storing the number. Analogous anonymization applies to `sms_messages.to_e164`; the `body` content is deleted or replaced with its length after the retention period.

**Data subject rights.** Right to erasure: search by number across all tables + immediate anonymization (procedure in the admin panel). Exception: `opt_outs` records are kept indefinitely (basis: the obligation to respect an objection — GDPR Art. 21); only the number and date are stored, no metadata.

**Data location.** Application and database hosting: EU region (e.g. AWS `eu-central-1` / Hetzner). Twilio: configure **EU Region (Ireland, `ie1`)** for Voice/SMS processing wherever the features are available there; for functions processed in the US — Twilio's standard contractual clauses (SCCs), noted in the record of processing activities.

**Information obligation.** Layered approach: a short notice in the footer of the first SMS is neither required nor practical given the character limit; the client (controller) fulfills the obligation via a clause on their website / wherever the number is published. A clause template is provided to the client during onboarding.

---

## 5. Database structure (PostgreSQL)

### 5.1 Relationships

```
clients 1 ──── N twilio_numbers
   │ 1                │ 1
   │                  │
   ├──── N ai_prompts │
   │ 1                │
   ├──── N call_events N (FK: client_id, twilio_number_id)
   │ 1        │ 1
   │          └──── N sms_messages
   ├──── N opt_outs
   └──── N audit_log_entries
```

### 5.2 DDL

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "citext";

CREATE TABLE clients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name        TEXT NOT NULL,
    email               CITEXT UNIQUE NOT NULL,
    owner_phone_e164    VARCHAR(16) NOT NULL CHECK (owner_phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    status              TEXT NOT NULL DEFAULT 'trial'
                        CHECK (status IN ('trial', 'active', 'suspended', 'cancelled')),
    country_code        CHAR(2) NOT NULL DEFAULT 'PL' CHECK (country_code ~ '^[A-Z]{2}$'),
                        -- selects the CountryRules (section 4.0) used for this client
    daily_sms_limit     INT  NOT NULL DEFAULT 100,
    log_retention_days  INT  NOT NULL DEFAULT 90,
    anonymization_salt  TEXT NOT NULL DEFAULT encode(gen_random_bytes(16), 'hex'),
    -- Enterprise hook, unused by SMB clients: an account tier flag lets the
    -- app branch on stricter behavior (e.g. mandatory audit logging, RAG
    -- access-group enforcement) without a schema change when the first
    -- enterprise client signs. NULL/'standard' behaves exactly like v1.0.
    account_tier        TEXT NOT NULL DEFAULT 'standard'
                        CHECK (account_tier IN ('standard', 'enterprise')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE twilio_numbers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     UUID NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    -- Generic E.164 shape, not PL-only (+48...): a client's number pool comes
    -- from whichever country their `clients.country_code` names (section 4.0).
    phone_e164    VARCHAR(16) UNIQUE NOT NULL CHECK (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
    twilio_sid    VARCHAR(64) UNIQUE NOT NULL,          -- PNxxxxxxxx...
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_twilio_numbers_lookup ON twilio_numbers (phone_e164) WHERE is_active;

CREATE TABLE ai_prompts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id         UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    system_prompt     TEXT NOT NULL,        -- assistant persona, company data, tone, prohibitions (marketing, PII)
    fallback_message  TEXT NOT NULL,        -- static emergency SMS (AI timeout), already GSM-7 safe
    allow_diacritics  BOOLEAN NOT NULL DEFAULT false,
    max_sms_segments  INT NOT NULL DEFAULT 1 CHECK (max_sms_segments BETWEEN 1 AND 3),
    -- Phase 2 hook (section 9): off by default, so Phase 1 clients are
    -- byte-for-byte unaffected. When true, ai_client.generate() runs a
    -- retrieval step against knowledge_chunks before calling the model.
    rag_enabled       BOOLEAN NOT NULL DEFAULT false,
    version           INT NOT NULL DEFAULT 1,
    is_active         BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- exactly one active prompt per client; older versions remain as history
CREATE UNIQUE INDEX uq_ai_prompts_active ON ai_prompts (client_id) WHERE is_active;

CREATE TABLE call_events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_sid          VARCHAR(64) UNIQUE NOT NULL,      -- webhook idempotency
    client_id         UUID REFERENCES clients(id),      -- NULL when number is orphaned
    twilio_number_id  UUID REFERENCES twilio_numbers(id),
    caller_e164       VARCHAR(16),                      -- NULL: anonymous or after anonymization
    caller_hash       CHAR(64),                         -- SHA-256(number + client salt)
    forwarded_from    VARCHAR(16),
    status            TEXT NOT NULL DEFAULT 'received' CHECK (status IN (
                        'received','no_tenant','client_suspended','anonymous','invalid_number',
                        'loop_detected','opted_out','non_mobile','deduplicated','limit_exceeded',
                        'sms_queued','sms_sent','sms_failed')),
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    anonymized_at     TIMESTAMPTZ
);
CREATE INDEX idx_call_events_client_time ON call_events (client_id, received_at DESC);
CREATE INDEX idx_call_events_dedup ON call_events (client_id, caller_e164, received_at DESC)
    WHERE caller_e164 IS NOT NULL;

CREATE TABLE sms_messages (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_event_id  UUID NOT NULL REFERENCES call_events(id),
    client_id      UUID NOT NULL REFERENCES clients(id),
    message_sid    VARCHAR(64) UNIQUE,                  -- SMxxxx; NULL until accepted by Twilio
    to_e164        VARCHAR(16),
    body           TEXT,
    encoding       TEXT NOT NULL DEFAULT 'gsm7' CHECK (encoding IN ('gsm7','ucs2')),
    segments       INT  NOT NULL DEFAULT 1,
    is_fallback    BOOLEAN NOT NULL DEFAULT false,      -- true = a template was used instead of AI
    status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                     ('queued','sent','delivered','undelivered','failed')),
    error_code     TEXT,
    price_usd      NUMERIC(8,5),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_sms_messages_client_time ON sms_messages (client_id, created_at DESC);

CREATE TABLE opt_outs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    phone_e164  VARCHAR(16) NOT NULL,
    source      TEXT NOT NULL DEFAULT 'sms_stop' CHECK (source IN ('sms_stop','manual','twilio_21610')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, phone_e164)
);

-- Enterprise hook: append-only audit trail. Cheap to write for every client
-- (one INSERT, no FK cascade risk), but only surfaced in the admin panel /
-- required contractually for account_tier = 'enterprise'. Nothing here
-- blocks the SMB flow — writing to this table is fire-and-forget from the
-- worker (section 3.4) and never gates a send.
CREATE TABLE audit_log_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id   UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    actor       TEXT NOT NULL,          -- 'system' | 'worker' | admin user id
    action      TEXT NOT NULL,          -- e.g. 'sms_sent', 'prompt_updated', 'gdpr_erasure'
    ref_type    TEXT,                   -- 'call_event' | 'sms_message' | 'ai_prompt' | ...
    ref_id      UUID,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_log_client_time ON audit_log_entries (client_id, created_at DESC);
```

Design notes: `call_events` records **every** webhook event (including filtered ones) — the `status` field is simultaneously the pipeline's outcome and material for statistics and debugging; the unique constraint on `call_sid` provides idempotency on Twilio webhook retries (`INSERT ... ON CONFLICT (call_sid) DO NOTHING` — no insert means a duplicate, and the handler ends).

---
## 6. Technology stack and project structure

### 6.1 Recommended stack

Python is the right choice here — the system's profile is I/O-bound webhooks + AI integration, where Python's ecosystem (Twilio SDK, `phonenumbers`, LLM clients, Celery) is the most mature. The Node.js/TypeScript alternative (Fastify + BullMQ) is technically on par; there's no clear advantage, and if Phase 1 (AI) is already in Python, a shared language eliminates duplicated prompt logic.

| Layer | Technology | Rationale |
|---|---|---|
| API / webhooks | **Python 3.12 + FastAPI + Uvicorn** | async I/O, Pydantic validation, < 1s per webhook |
| Task queue | **Celery 5 + Redis 7** | retry with backoff, beat (GDPR jobs), isolating AI failures from webhooks |
| Database | **PostgreSQL 16 + SQLAlchemy 2 + Alembic** | constraints from section 5, migrations; `pgvector` extension added in Phase 2 (section 9) — no migration off Postgres needed |
| Cache / limits | **Redis** (same instance, separate DB) | cooldown, daily counters, Lookup cache |
| Telephony | **twilio** (SDK), **phonenumbers** | signature validation, E.164 |
| HTTP to AI | **httpx** | hard timeouts, connection pooling |
| Deployment | **Docker Compose** → 1 VPS in the EU (Hetzner/AWS eu-central-1) to start | GDPR: data in the EU; vertical scaling suffices for a long while |
| Observability | **Sentry + structlog** (JSON) + Prometheus metrics | section 7.9 |

### 6.2 Project structure

```
mctb-rag/
├── app/
│   ├── main.py                  # FastAPI app factory, routers
│   ├── config.py                # Pydantic Settings (ENV)
│   ├── api/
│   │   ├── webhooks.py          # POST /webhooks/twilio/{voice,sms-status,inbound-sms}
│   │   └── admin.py             # client onboarding, prompts, GDPR (data erasure)
│   ├── core/
│   │   ├── security.py          # X-Twilio-Signature validation (FastAPI dependency)
│   │   ├── auth.py              # admin-panel auth as a swappable FastAPI dependency —
│   │   │                        #   API key today, drop-in for OAuth/OIDC/SSO later
│   │   │                        #   without touching the routes that depend on it
│   │   ├── countries.py         # CountryRules registry (section 4.0) — PL is the only entry today
│   │   ├── phone.py             # normalize_e164, is_anonymous, is_mobile_number(phone, country_code)
│   │   └── sms_encoding.py      # GSM7_SAFE, prepare_sms_body(..., country_code=...), segment counter
│   ├── services/
│   │   ├── tenant_resolver.py   # To → twilio_numbers → client + prompt (60s cache)
│   │   ├── guards.py            # cooldown, daily limits, opt-out, loops
│   │   ├── ai_client.py         # Phase 1 adapter (httpx, timeout, circuit breaker);
│   │   │                        #   Phase 2 (section 9) adds an internal retrieval call
│   │   │                        #   here when ai_prompts.rag_enabled is true
│   │   ├── retriever.py         # Phase 2 (section 9): embed query, pgvector similarity search
│   │   ├── audit.py             # audit_log.record(...) — thin wrapper around
│   │   │                        #   audit_log_entries, fire-and-forget, never blocks a send
│   │   └── sms_sender.py        # Twilio Messages API + status_callback
│   ├── models/                  # SQLAlchemy: client, twilio_number, ai_prompt, call_event,
│   │   │                        #   sms_message, opt_out, audit_log_entry, (Phase 2: knowledge_chunk)
│   ├── db.py                    # SQLAlchemy engine/session factory (API + Celery + Alembic)
│   ├── workers/
│   │   ├── celery_app.py
│   │   ├── tasks.py             # process_missed_call
│   │   └── beat.py              # GDPR anonymization (nightly), counter reset
│   └── migrations/              # Alembic
├── tests/
│   ├── test_sms_encoding.py     # golden tests for transliteration and segments
│   ├── test_webhooks.py         # signatures, idempotency, TwiML
│   └── test_guards.py           # loops, dedup, limits
├── docker-compose.yml           # api, worker, beat, postgres, redis
├── Dockerfile
├── .env.example
└── pyproject.toml
```

### 6.3 Environment variables (`.env.example`)

```bash
DATABASE_URL=postgresql+psycopg://mctb:***@db:5432/mctb
REDIS_URL=redis://redis:6379/0
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=***                  # also used to validate webhook signatures
TWILIO_REGION=ie1                      # EU region (GDPR)
PUBLIC_BASE_URL=https://api.example.pl # for URL reconstruction during signature validation
AI_SERVICE_URL=http://ai-service:8080  # Phase 1
AI_TIMEOUT_SECONDS=8
EMBEDDING_SERVICE_URL=                 # Phase 2 (section 9) — empty/unused until then
SENTRY_DSN=
```

---

## 7. Security and edge cases

### 7.1 Twilio webhook signature validation (mandatory)

Every request to `/webhooks/twilio/*` must carry a valid `X-Twilio-Signature` header (HMAC-SHA1 with the auth token). Without this validation, anyone who knows the URL could generate SMS messages at the platform's expense.

```python
from fastapi import Request, HTTPException
from twilio.request_validator import RequestValidator

validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)


async def verify_twilio_signature(request: Request) -> dict:
    form = dict(await request.form())
    # Behind a reverse proxy we reconstruct the public URL — the signature is
    # computed over it, not over the internal http://api:8000/...
    url = f"{settings.PUBLIC_BASE_URL}{request.url.path}"
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, form, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return form
```

Additionally: webhook endpoints only over HTTPS; a rate limit at the reverse-proxy level (e.g. 20 req/s per IP) as a second line of defense.

### 7.2 Protection against message/call loops

Loop scenarios and their blocks (all end with a `loop_detected` status, zero sends):

| Scenario | Block |
|---|---|
| The caller is a Twilio number from our own system (e.g. two MCTB-RAG clients call each other and both don't answer) | `From` checked against the full `twilio_numbers` table (Redis-cached set, refreshed on provisioning) |
| The client calls their own Twilio number "to test it," or `ForwardedFrom == From` | `From == clients.owner_phone_e164` or `From == ForwardedFrom` → drop |
| The reply SMS lands on a number with SMS→voice forwarding at an exotic carrier | one-shot design: SMS is sent only in reaction to a `call_event`, never in response to an inbound SMS |
| Repeated calls from the same number (deliberately running up costs) | cooldown from 7.3 + daily limit per client |

### 7.3 Anti-spam: deduplication and limits

**Cooldown per (client, caller) pair.** One SMS per pair within a 4h window — atomic in Redis, no race condition when two calls land in the same second:

```python
def acquire_cooldown(client_id: str, caller: str, ttl: int = 14400) -> bool:
    # SET NX: True only for the first event in the window
    return bool(redis.set(f"cooldown:{client_id}:{caller}", 1, nx=True, ex=ttl))
```

**Daily limit per client** (`clients.daily_sms_limit`, default 100): a Redis `INCR` counter with a TTL until midnight Europe/Warsaw; exceeding it → `limit_exceeded` status, ops alert, and an email to the client. Protects the Twilio budget against a war-dialing attack on the client's number.

**Global safety limit** (cost circuit breaker): if the platform-wide SMS-per-hour total exceeds a threshold (e.g. 5x the 7-day average) — pause sending and trigger a PagerDuty alert. Protects against our own bugs (e.g. a loop introduced by a deploy).

### 7.4 Anonymous calls and undeliverable numbers

- `From` ∈ {`anonymous`, `unknown`, `restricted`, `+266696687`} (Twilio's magic value for CLIR) → `anonymous` status, no SMS, the event is visible in the client's stats as "call from a withheld number."
- **Landline numbers** can't receive SMS. Two-stage detection: (1) cheap heuristic — Polish mobile numbers start with prefixes `45x, 50x, 51x, 53x, 57x, 60x, 66x, 69x, 72x, 73x, 78x, 79x, 88x`; a `+48` number outside this list → `non_mobile` status; (2) optionally Twilio Lookup (`line_type_intelligence`) with a 30-day Redis cache — enable when the Lookup cost < the cost of falsely sent SMS. Additionally, error codes `21614`/`30006` from the status callback add the number to the non-mobile cache.

### 7.5 AI timeout and failure

Overarching rule: **an AI failure never blocks the SMS** — the caller gets a reply either way.

- `httpx` with a hard `AI_TIMEOUT_SECONDS=8` timeout (2s connect, 6s read). No retry to AI within the same event — a retry would mean an SMS several minutes later, which hurts UX; instead, an immediate **fallback**: `ai_prompts.fallback_message` (static, verified as GSM-7-safe when saved in the panel), `sms_messages.is_fallback = true`.
- **Circuit breaker**: ≥ 5 consecutive AI errors within 60s → open the circuit for 2 minutes (all events go straight to fallback, without waiting for a timeout) + alert. After 2 minutes, a single trial request (half-open).
- A `fallback_rate` metric on the dashboard — an increase > 5% is an incident.
- Phase 2 note (section 9): the same rule applies to the retrieval step — if `retriever.py` times out or errors, generation proceeds **without** retrieved context rather than failing the whole request. Retrieval is an enhancement, never a hard dependency.

### 7.6 Idempotency and retries

- **Voice webhook**: Twilio retries the webhook if it doesn't get a 2xx. `INSERT ... ON CONFLICT (call_sid) DO NOTHING` — a duplicate doesn't create a second job.
- **Celery task**: `max_retries=3`, `retry_backoff=True` (2s → 4s → 8s), only for transient errors (DB, network, Twilio 5xx/429). Before sending the SMS, the task checks whether the `call_event` already has an `sms_messages` row — a retry after partial execution won't duplicate the message.
- **Twilio Messages API**: 4xx errors (e.g. `21211` invalid number) — no retry, log + `sms_failed` status; `429/5xx` — retried via the Celery mechanism.

### 7.7 Application security

- Secrets only in ENV/secret manager; the Twilio auth token never appears in logs (structlog filter).
- Admin panel: auth goes through `app/core/auth.py` (section 6.2) as a single FastAPI dependency — API key for the MVP, swappable for OAuth/OIDC/SSO for an enterprise client without touching route handlers. GDPR endpoints (data erasure) additionally write to `audit_log_entries` (section 5.2).
- AI-generated content is sent on the client's behalf — the system prompt carries hard prohibitions: no caller PII in the body, no promised deadlines, no marketing content, no links (links in an SMS from an unknown number look like phishing and hurt delivery rates).
- PostgreSQL backups encrypted, stored in the EU, backup retention ≤ data retention + 30 days (consistent with GDPR).

### 7.8 Monitoring and alerts

| Metric | Alert threshold |
|---|---|
| 5xx responses on webhooks | > 1% over 5 min |
| Webhook response time p95 | > 800 ms |
| `fallback_rate` (SMS from template instead of AI) | > 5% over 15 min |
| `sms_failed` / sent | > 3% over 1 h |
| Platform-wide SMS volume/h | > 5x the 7-day average |
| `no_tenant` events (orphan number) | ≥ 1 (provisioning bug) |

---

## 8. MVP scope and rollout order (Phase 1)

1. **Week 1:** models + migrations (section 5), Voice webhook with signature validation and TwiML `<Reject/>`, `call_sid` idempotency.
2. **Week 2:** Celery worker with the full guard chain (3.4), `sms_encoding` module with golden tests, SMS sending + status callback.
3. **Week 3:** AI integration (Phase 1) with timeout, fallback, and circuit breaker; inbound SMS (STOP/KONIEC); GDPR jobs (anonymization).
4. **Week 4:** admin panel (onboarding, prompts, per-client log view), monitoring, end-to-end tests on a production number.

Phase 1 is feature-complete and deployable to a real client without section 9. Section 9 is the next phase, started only after Phase 1 is stable in production.

---

## 9. Phase 2 — RAG (Retrieval-Augmented Generation)

### 9.1 Goal

Today, `ai_prompts.system_prompt` is a single static text field — everything the assistant "knows" about the client's business (hours, pricing, services, policies) has to be hand-written into one prompt and manually kept up to date. Phase 2 lets a client upload source documents (PDF, plain text, pasted FAQ) once; the system retrieves the most relevant snippets per incoming call and folds them into the generation context, instead of stuffing everything into the system prompt every time.

This is additive: `ai_prompts.rag_enabled` (section 5.2) defaults to `false`. Phase 1 clients are completely unaffected until a client is explicitly switched on.

### 9.2 New tables

```sql
CREATE EXTENSION IF NOT EXISTS "vector";  -- pgvector

CREATE TABLE knowledge_documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    -- Enterprise hook: nullable today, always NULL for SMB clients. When an
    -- enterprise client needs "different staff see different documents"
    -- (e.g. franchise locations, internal vs. public FAQ), this groups
    -- documents without a schema change — the retriever (9.4) already
    -- filters on it.
    access_group_id UUID,
    source_type   TEXT NOT NULL CHECK (source_type IN ('pdf', 'text', 'faq_pair')),
    title         TEXT NOT NULL,
    raw_text      TEXT NOT NULL,          -- extracted, pre-chunking
    status        TEXT NOT NULL DEFAULT 'processing'
                  CHECK (status IN ('processing', 'ready', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_chunks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    client_id      UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,  -- denormalized for fast filtering
    access_group_id UUID,                 -- copied from the parent document, see above
    chunk_index    INT NOT NULL,
    content        TEXT NOT NULL,
    embedding      vector(1536) NOT NULL, -- dimension depends on the embedding model chosen
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_knowledge_chunks_client ON knowledge_chunks (client_id);
CREATE INDEX idx_knowledge_chunks_embedding ON knowledge_chunks
    USING hnsw (embedding vector_cosine_ops);
```

Every retrieval query filters on `client_id` first (`WHERE client_id = :client_id ORDER BY embedding <=> :query_embedding LIMIT 5`) — this is the tenant isolation boundary, same principle as every other table in section 5. `access_group_id` is a second, optional filter layered on top for the enterprise case; it costs nothing for clients who never set it.

### 9.3 Ingestion pipeline

1. Client uploads a document (or pastes FAQ text) via the admin panel → `knowledge_documents` row, `status = 'processing'`.
2. A Celery task: extract text (PDF → plain text), chunk it (~300–500 tokens per chunk, with overlap), call the embedding model per chunk, write rows to `knowledge_chunks`, flip `knowledge_documents.status = 'ready'`.
3. Re-ingestion on edit: delete the document's existing chunks and re-chunk, rather than trying to diff — documents here are small enough (business FAQ/pricing, not a legal corpus) that this is simpler and safer than incremental updates.

### 9.4 Retrieval integration

`app/services/retriever.py` (new):

```python
def retrieve(client_id: str, query: str, access_group_id: str | None, k: int = 5) -> list[str]:
    query_embedding = embed(query)  # same model used at ingestion time
    filters = "client_id = :client_id"
    if access_group_id is not None:
        filters += " AND (access_group_id = :access_group_id OR access_group_id IS NULL)"
    rows = db.execute(
        f"""
        SELECT content FROM knowledge_chunks
        WHERE {filters}
        ORDER BY embedding <=> :query_embedding
        LIMIT :k
    """,
        client_id=client_id,
        access_group_id=access_group_id,
        query_embedding=query_embedding,
        k=k,
    )
    return [r.content for r in rows]
```

`ai_client.generate()` (existing, section 6.2) changes to:

```python
def generate(prompt, context, timeout=8.0):
    extra_context = ""
    if prompt.rag_enabled:
        try:
            chunks = retriever.retrieve(prompt.client_id, query=context.caller_intent_hint, ...)
            extra_context = "\n".join(chunks)
        except (TimeoutError, DBError):
            pass  # retrieval is best-effort — see section 7.5
    return ai_service.call(prompt.system_prompt, extra_context, context, timeout=timeout)
```

No change to `process_missed_call` (section 3.4) is required — the call signature to `ai_client.generate()` stays the same.

### 9.5 Evaluation (don't skip this)

A RAG feature that "looks like it works" on three manual tests is not a portfolio-credible RAG feature. Minimum evaluation before calling Phase 2 done:

- A small labeled test set per client (~20–30 question/expected-answer pairs from real FAQ content).
- **Retrieval precision@k**: for each test question, is the chunk containing the right answer in the top-k retrieved?
- **Faithfulness / hallucination check**: does the generated SMS only assert things present in the retrieved chunks, or does it invent details (e.g. a price that isn't in any document)? Spot-check manually for the first client, then consider an LLM-as-judge pass for scale.
- Track `fallback_rate` and a new `retrieval_miss_rate` (queries where no chunk cleared a similarity threshold) on the same dashboard as section 7.8.

### 9.6 Rollout order (Phase 2)

1. `knowledge_documents` / `knowledge_chunks` migration + pgvector extension.
2. Ingestion pipeline (upload → chunk → embed) for one client, manually verified.
3. `retriever.py` + `ai_client.generate()` integration, `rag_enabled` flag wiring.
4. Evaluation set (9.5) for that client; only then turn `rag_enabled` on for others.
5. Admin panel: document upload/management UI.

---

## 10. Beyond Phase 2 (backlog)

Self-service client panel, client notifications about missed calls (push/email), two-way SMS conversation with AI, usage-based billing, Twilio Lookup, multi-agent classification/generation/validation split (evaluate only if a specific client need justifies the added latency/cost — see discussion on multi-agent vs. single-call generation for this SMS-length use case).