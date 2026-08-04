# 1. Shared database, shared schema for multi-tenancy

Status: Accepted (see spec.md section 2.2 for the full technical detail)

## Context

MCTB-RAG serves many clients (businesses) from one deployment. Each incoming call
must be routed to the right tenant by the destination (`To`) number. We need
to pick an isolation model before the schema in spec.md section 5 is set in
stone, since changing it later means a data migration across every table.

Options considered: (a) shared DB + shared schema with a `client_id` column
on every tenant-scoped table, (b) one Postgres schema per client, (c) one
database per client.

## Decision

Shared database, shared schema, `client_id UUID` on every tenant-scoped
table (`twilio_numbers`, `ai_prompts`, `call_events`, `sms_messages`,
`opt_outs`).

Reasoning: expected scale is hundreds to low thousands of clients with
webhook-driven, bursty traffic — not enough per-tenant data volume or
compliance requirement to justify the operational cost of per-schema or
per-database isolation (migrations x N schemas, connection pooling x N
databases). A `client_id` column plus consistent `WHERE client_id = :id`
filtering (and indexes like `idx_call_events_client_time`) gives adequate
isolation at this scale.

## Consequences

- Every query that touches a tenant-scoped table **must** filter by
  `client_id` — there is no schema-level boundary to fall back on if a
  filter is forgotten. This is the main risk of this choice; code review
  (human or agent) should treat a missing `client_id` filter on a new query
  as a bug, not a style nit.
- Onboarding a new client is just an `INSERT` into `clients` +
  `twilio_numbers` — no schema/DB provisioning step, which keeps the
  provisioning flow in spec.md section 2.3 simple.
- If a single client ever needs stronger isolation (e.g. a large customer
  with contractual data-residency requirements beyond "EU region"), that's a
  future migration, not a v1 concern — revisit this ADR if that comes up
  rather than special-casing it silently in code.

## Template for future ADRs

When a new architectural decision is made (a real one — not every PR needs
one), copy this file's structure: Context → Decision → Consequences, numbered
sequentially, one file per decision. Keep spec.md as the current-state source
of truth; ADRs exist so the *why* behind a past decision survives even after
spec.md itself is edited or superseded.
