# Product specification — custom-agent-sdk (Phase 0)

> Status: draft. A coding agent must not implement product code until this specification is approved through Genesis.

**Scope:** Stage 1, Phase 0 — the single-agent skeleton — plus the store-hardening requirements (FR-17..FR-21, NFR-8, AC-11..AC-15) that Phase 2 makes reachable. Those were added after Phase 0 was approved, measured rather than assumed: see the Phase 2 readiness section. Phases 2–8 each get their own Genesis specification when they start, per the design docs' refusal to write low-level design for components that do not exist yet. Source of truth: `agent_sdk/agent-sdk-low-level-design (1).md` (Phase 0 detail) and `agent_sdk/agent-sdk-master-design-v0.3.md` (design of record).

## Problem

Building multi-agent products on a vendor agent SDK couples every product to one provider's harness, and leaves orchestration state, tool authorization, and evidence provenance as opaque vendor internals. This project builds the harness in-house so those three things are inspectable, portable, and governable.

Phase 0 does not solve that problem. Phase 0 proves the foundation the solution stands on: one agent, one loop, one tool call, fully persisted, fully provenance-tagged, fully tenant-scoped — with the interface seams that Phases 2–8 need already cut, so later phases add implementations rather than replace contracts.

## Users

- **Primary:** the developer building Stage 2's three products (Research Analyst, Software Tester, Agentic RAG) against this SDK's public API.
- **Affected:** operators debugging a run after the fact from persisted state and events; reviewers who must answer "exactly what configuration produced this run" without re-running it.
- **Not a user in Phase 0:** any end user of a Stage 2 product. Phase 0 ships no product.

## Functional requirements

- FR-1: `Runner.run(spec, task, config)` drives one agent to a terminal status of `completed`, `failed`, or `max_turns_exceeded`, and returns a `RunResult` carrying status, output, events, and usage.
- FR-2: Canonical primitives `Message`, `Role`, `ToolCall`, `ToolResult` exist provider-independently, and every `ToolResult` carries exactly one `ContentProvenance` (origin, instruction_authority, trust_zone, taint_flags, source_uri_or_hash).
- FR-3: A `ModelClient` protocol with `send(ModelRequest) -> ModelResponse`, implemented by an OpenAI-compatible adapter that calls the configured `BASE_URL` endpoint and maps its response to `ModelResponse` (message, tool_calls, stop_reason, usage, provider_response_id, provider_metadata).
- FR-4: `ToolSpec` (name, description, input_schema, risk_class, read_only, idempotent, approval_policy) and a `ToolRegistry` supporting registration and lookup; duplicate registration raises at registration time, not at call time.
- FR-5: `ToolExecutor` implements the fixed lifecycle — resolve, validate arguments, permission check, approval (auto-allow stub), `before_tool` hook, execute, assign provenance, `after_tool` hook, emit `ToolCalled` — returning a `ToolExecutionOutcome`.
- FR-6: A `PermissionChecker` protocol taking `(tool_call, principal_context)`, with `AllowlistPermissionChecker` as the default implementation.
- FR-7: A minimal `ContextAssembler` builds `ModelRequest` from message history and tool schemas, carrying provenance as request metadata rather than as model-readable instruction text.
- FR-8: A `RuntimeHook` interface with `before_model`, `after_model`, `before_tool`, `after_tool`, wired into `AgentLoop` and `ToolExecutor`, defaulting to `continue` at every point.
- FR-9: A `SessionStore` protocol with Postgres implementation exposing `append(run_id, message)` and `history(run_id)`; `sequence_no` is assigned inside the same transaction as the insert.
- FR-10: `RunEvent` records are emitted and persisted with the full envelope (event_id, schema_version, sequence_no, event_type, tenant/project/run ids, nullable agent/task/tool_call/attempt ids, parent_event_id, correlation_id, timestamp, payload) for the Phase 0 event set: `RunStarted`, `ModelCalled`, `ToolCalled`, `RunCompleted`, `RunFailed`.
- FR-11: Exactly one `ExecutionManifest` row is written per run at start, capturing sdk_version, agent_spec hash, instructions_hash, model id/version/adapter_version, tool_spec_hashes, and policy_version.
- FR-12: A `ModelRegistry` records provider, model_id, model_version, adapter_version, and capabilities (max_context_tokens, supports_parallel_tool_calls, cost_per_token).
- FR-13: The error taxonomy exists as a hierarchy: `AgentSDKError` → `ModelError` (Timeout, RateLimited, ProviderUnavailable, InvalidStructuredOutput) / `ToolError` (NotFound, ValidationError, PermissionDenied, ApprovalRequired, Timeout, ExecutionError) / `WorkflowError` (BudgetExceeded, MaxTurnsExceeded, MaxDepthExceeded, DependencyFailed, ReplanLimitExceeded, Cancelled).
- FR-14: `AgentLoop` terminates at `max_turns` with status `max_turns_exceeded` as a defined terminal state, never as an exception escaping to application code.
- FR-15: `ModelError.Timeout` and `ModelError.RateLimited` retry up to 2 times with exponential backoff; every other `ModelError` propagates immediately without retry.
- FR-16: Future-seam types are defined but only minimally implemented: `ToolExecutionOutcome` (only `Completed` and `Failed` reachable), `RunInterruption` (type only, no persistence), `ModelRequest.output_schema` (always `None`), `PrincipalContext` (persisted, never read).

### Phase 2 readiness — store hardening

Phase 2 introduces parallel subagents, which makes three Phase 0 assumptions
reachable that Phase 0 could legitimately assume away. Each requirement below
was written against a measured failure, not a suspected one; the measurements
are recorded in `.genesis/project.json` (KNOWLEDGE-e52fb4dc, KNOWLEDGE-010d12d3).

- FR-17: The schema has a versioned migration path. Applying migrations to a database created by an earlier schema brings it to the current version, records which migrations ran, and is idempotent. `apply_schema`'s `CREATE TABLE IF NOT EXISTS` is a no-op for anything new on an existing database, so today a column added to `schema.sql` is silently not created and the code fails later at insert time. FR-17 is ordered first because FR-18 and FR-21 both need a schema change to reach an existing database.
- FR-18: `run_events.sequence_no` is assigned inside the insert from the stored maximum for that run, as `messages.sequence_no` already is — never from an in-process counter. A second event sink for one run (a subagent, or any resumed run) must continue the sequence rather than restart it.
- FR-19: Concurrent appends to one run are available, not merely safe. A losing writer to `messages` or `run_events` retries and commits; no caller ever receives a raw driver exception for a write that a retry would have completed.
- FR-20: Persistence does not block the event loop. Store calls issued from async code use non-blocking I/O and a connection pool, replacing the per-call connect/authenticate/close cycle.
- FR-21: A run may record the run that spawned it, so a subagent's trace reconstructs together with its parent's. `runs` has no such column today.

## Non-functional requirements

- NFR-1: Provider-agnostic. Switching between `openai.*`, `bedrock.*`, `azure.*`, and `vertex_ai.*` models is a configuration change only — no SDK source file changes, no new adapter.
- NFR-2: Multi-tenant by construction. Every row in every table carries non-null, indexed `tenant_id` and `project_id`.
- NFR-3: Auditable. Any completed run reconstructs from `runs` + `messages` + `run_events` together. Events alone are explicitly not the source of truth.
- NFR-4: No credential ever reaches model context, a persisted row, or a `RunEvent` payload.
- NFR-5: The public API surface is `AgentSpec`, `RunConfig`, `Runner`, `RunResult` only. No internal collaborator is imported by application code or by the golden eval.
- NFR-6: No runtime dependency on LangGraph, Claude Agent SDK, or OpenAI Agents SDK. The SDK must import and run with none of them installed.
- NFR-7: Phase 0 interfaces are chosen so Phases 2–6 add fields and implementations rather than replace contracts.
- NFR-8: Persistence is not the concurrency ceiling. With several runs executing concurrently against a model client that performs no network I/O, the worst single event-loop stall stays under 50 ms, and wall-clock time stays within 3x the same workload run entirely in memory. Measured today: 1008 ms worst stall and roughly 30x wall time.

## Constraints

- Python with async/await, matching the existing FastAPI/LangGraph stack.
- PostgreSQL is the sole durable backend; one database, multiple tables, no separate infrastructure per store.
- The model endpoint is the configured `BASE_URL` LiteLLM gateway, authenticated by `MODEL_API_KEY` from `.env`. Both are read from environment, never committed.
- The gateway blocks `GET /v1/models` at the Envoy layer (403 "fault filter abort"); the working model-listing path is `GET /models`. `POST /v1/chat/completions` works normally.
- Windows development host: PostgreSQL 16 native service on localhost:5432, Docker daemon currently not running.
- Personal product development (ADR-04) — no external review gating.

## Non-goals

- No subagents, orchestrator, DAG, or plan versioning (Phase 2).
- No evidence ledger, claims, or critic (Phase 3).
- No MCP, tool catalog, credential brokering, or approval manager (Phase 4).
- No sandbox or workspace (Phase 5).
- No durable interruptions, replay, or idempotency (Phase 6).
- No context compaction (Phase 7).
- No OpenTelemetry export or compatibility enforcement (Phase 8).
- No streaming of model tokens, and no `RunHandle`.
- No structured-output enforcement beyond the unused schema slot.
- None of the three Stage 2 products is built or started.

## Acceptance criteria

- AC-1: The golden eval runs to `completed` — an agent with one registered `echo` tool is asked to call it three times with different inputs and summarize the results, and does so.
- AC-2: In that same run, a request for a tool outside the allowlist produces `Failed(ToolError.PermissionDenied)`, is surfaced to the model as an error tool result, and the run still reaches `completed`.
- AC-3: In that same run, an `echo` call with a malformed argument produces `Failed(ToolError.ValidationError)` at the validate step, and the tool implementation is provably never invoked.
- AC-4: Every `ToolResult` persisted by the run carries a non-null, fully populated `ContentProvenance`.
- AC-5: Every row written across `runs`, `messages`, `run_events`, and `execution_manifests` has non-null `tenant_id` and `project_id`.
- AC-6: `execution_manifests` holds exactly one row for the run, with every field populated.
- AC-7: The run's execution trace reconstructs in order from `runs` + `messages` + `run_events` read back from Postgres.
- AC-8: A run configured with a deliberately low `max_turns` terminates with status `max_turns_exceeded` and raises nothing to the caller.
- AC-9: The same golden eval passes unchanged against two model ids from different upstream providers (`openai.gpt-4o-mini` and `bedrock.anthropic.claude-haiku-4-5`), with no source change between runs.
- AC-10: No persisted row and no event payload anywhere in the database contains the value of `MODEL_API_KEY`.

### Phase 2 readiness

Each of these is a command, not a claim. AC-12 through AC-14 are the probe that
found the defects, turned into assertions.

- AC-11: Applying migrations to a database whose schema predates them brings it to the current version and creates the columns FR-18 and FR-21 need; applying them a second time changes nothing and reports the same version.
- AC-12: Two independent event sinks bound to one run each emit an event, both rows commit, and the run's `sequence_no` values are unique and contiguous. Today the second sink fails with `UniqueViolation` and one of the two events is lost.
- AC-13: Twelve concurrent writers appending to one run all commit, with unique contiguous sequence numbers and no exception reaching any caller. Today 9 of 12 commit and 3 raise `UniqueViolation`.
- AC-14: Six runs executing concurrently against a no-network model client satisfy NFR-8's stall and wall-clock bounds, both measured in the same process as an in-memory baseline so the comparison is independent of machine speed.
- AC-15: A run that records a parent run reconstructs together with it, and both rows carry the tenant and project of the run that owns them (ADR-11 is not relaxed for child runs).

## Risks

- **Provider neutrality is only half-proven.** AC-9 switches upstream providers, but through one gateway speaking one wire format. That proves model-agnosticism, not wire-format-agnosticism; a genuinely second wire format remains unproven until a non-LiteLLM adapter exists. Mitigation: state the limit honestly now, keep `ModelClient` the only place provider shape is known, and treat the second wire format as Phase 0/1's real exit criterion rather than pretending Phase 0 closed it.
- **Postgres credentials are unresolved.** The service is up but its password is not known to the harness, so no gate touching the database can pass yet. Mitigation: resolve before the first database-backed task is activated; it blocks AC-4 through AC-7.
- **Gateway reachability is partly filtered.** `GET /v1/*` is blocked by Envoy today. If the filter later extends to POST paths, every model-backed gate fails for reasons unrelated to the code. Mitigation: keep a scripted stub `ModelClient` so loop logic stays testable without the network.
- **Unused seams may be designed wrong.** `RunInterruption`, `ToolExecutionOutcome`'s unreachable variants, and `PrincipalContext` are specified against needs no Phase 0 code exercises. Mitigation: accepted deliberately and bounded — each is a type or a column, not a subsystem.
- **A connection pool is a new runtime dependency.** FR-20 most likely needs `psycopg_pool` (via `psycopg[binary,pool]`) and touches every store method. Mitigation: NFR-6 forbids vendor agent SDKs, not infrastructure libraries, and `psycopg_pool` is maintained by the psycopg project itself — but the choice is a decision to record, not a detail to slip in.
- **Hardening an approved milestone can regress it.** FR-17 through FR-21 change `postgres.py`, which M5 took nine review rounds to approve. Mitigation: the existing 423 tests are the regression gate and must stay green unchanged; any test that has to be edited to accommodate a change is a signal to re-examine the change, not the test.
- **A single golden eval carrying three assertions may pass for the wrong reason.** Mitigation: assert each path independently rather than asserting only the final status.

## Open questions

- ADR-06 (budget: hard ceiling + per-child reservation + reclaim) remains `Proposed`. Not blocking — it first bites in Phase 2.
- ADR-18 (relationship to the existing production RAG design) remains open. Not blocking — Track B only.
- The Postgres connection string and credentials for the local PostgreSQL 16 service.
- Which model id becomes the Phase 0 default, given the gateway exposes 308.
- Whether the second wire format (Phase 0/1) should be Anthropic-native Bedrock or something else, now that the gateway already reaches Bedrock models over the OpenAI shape.
- Resolved 2026-09-09, recorded rather than dropped: Phase 0/1's second wire format is DEFERRED past this hardening milestone. The gateway was found to serve the Anthropic Messages format at `/v1/messages` with the existing credential, tool round trip included, so the work is cheap whenever it is picked up; and the canonical contract was audited field by field and absorbs that shape, with two additive gaps (`Usage` cannot represent cached tokens, `Message.content` is a single string against Anthropic's content blocks). Provider neutrality therefore stays half-proven by choice, not by oversight.
