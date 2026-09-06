# Custom Agent SDK — Low-Level Design (LLD)

**Scope:** Phase 0 only. LLD for Phase 2 onward gets produced when that phase actually starts — writing detailed low-level design for phases 2–8 now would mean specifying implementation detail for components that don't exist yet and whose exact shape may still shift, which is the same over-engineering risk this whole design process has deliberately avoided at every stage.

**Explicitly no code in this document** — interfaces are specified as field lists and behavioral contracts; sequence flows are numbered prose. This is the last design artifact before implementation begins.

---

## 1. Phase 0 Boundary

**In scope:** `Message`/`ToolCall`/`ToolResult`/`ContentProvenance`, `ModelRequest`/`ModelResponse`, `AgentSpec`/`RunConfig`/`Runner`/`RunResult`, `ContextAssembler` (minimal), `Tool`/`ToolSpec`/`ToolRegistry`, `ToolExecutor`, `PermissionChecker`, `RuntimeHook` (minimal), `ModelClient`/`BedrockModelClient`, `ModelRegistry`, `ExecutionManifest` (lean), `SessionStore` (Postgres), `AgentLoop`, error taxonomy, `RunEvent`.

**Explicitly out of scope:** subagents, DAG/orchestration, MCP, sandbox, durable approvals/interruptions, context compaction, structured output enforcement beyond the schema slot existing, full OpenTelemetry export.

---

## 2. Data Model (Postgres)

All tables share two non-negotiable columns per ADR-11: `tenant_id`, `project_id` — both `NOT NULL`, both indexed.

### 2.1 `runs`

| Column | Type | Notes |
|---|---|---|
| `run_id` | UUID, PK | |
| `tenant_id` | text, NOT NULL | |
| `project_id` | text, NOT NULL | |
| `agent_spec_id` | text, NOT NULL | |
| `status` | enum: `running`/`completed`/`failed`/`max_turns_exceeded` | |
| `principal_context` | JSONB, nullable | lean metadata only in Phase 0, unread by any consumer yet |
| `max_turns` | int | |
| `model_id` | text | |
| `started_at` | timestamptz | |
| `completed_at` | timestamptz, nullable | |

Index: `(tenant_id, project_id, started_at)` for tenant-scoped listing queries.

### 2.2 `messages`

Conversation history — insert-only, never updated or deleted in Phase 0.

| Column | Type | Notes |
|---|---|---|
| `message_id` | UUID, PK | |
| `run_id` | UUID, FK → runs | |
| `tenant_id` | text, NOT NULL | denormalized for query isolation without a join |
| `sequence_no` | int | strictly increasing per `run_id`; defines read order |
| `role` | enum: `user`/`assistant`/`tool` | |
| `content` | text, nullable | |
| `tool_calls` | JSONB, nullable | array of `ToolCall` |
| `tool_results` | JSONB, nullable | array of `ToolResult`, each carrying `ContentProvenance` |
| `created_at` | timestamptz | |

Index: `(run_id, sequence_no)` — the read pattern for `SessionStore.history()` is always "all messages for this run, in order."

### 2.3 `run_events`

| Column | Type | Notes |
|---|---|---|
| `event_id` | UUID, PK | |
| `schema_version` | int | starts at 1 |
| `sequence_no` | int | strictly increasing per `run_id` |
| `event_type` | text | `RunStarted`/`ModelCalled`/`ToolCalled`/`RunCompleted`/`RunFailed` in Phase 0 |
| `tenant_id`, `project_id`, `run_id` | as above | |
| `agent_id`, `task_id`, `tool_call_id`, `attempt_id` | text, nullable | unpopulated in Phase 0 (no subagents/plan nodes yet); columns exist so Phase 2+ doesn't alter this table's shape |
| `parent_event_id`, `correlation_id` | UUID, nullable | |
| `timestamp` | timestamptz | |
| `payload` | JSONB | |

Index: `(run_id, sequence_no)`.

**Invariant carried from the HLD:** this table is the ordered audit stream, never the sole source of execution truth — `runs` + `messages` together are authoritative; `run_events` is what you replay to reconstruct *why*, not *what current state is*.

### 2.4 `execution_manifests`

One row per run, written once at `Runner.run()` start.

| Column | Type |
|---|---|
| `run_id` | UUID, PK, FK → runs |
| `sdk_version` | text |
| `agent_spec_hash` | text |
| `instructions_hash` | text |
| `model_id`, `model_version`, `model_adapter_version` | text |
| `tool_spec_hashes` | JSONB array |
| `policy_version` | text |
| `created_at` | timestamptz |

No enforcement logic reads this table in Phase 0 — it's written for future compatibility gating (Phase 8) and for being able to answer "exactly what configuration produced this run" during debugging, starting now.

### 2.5 `model_registry`

| Column | Type |
|---|---|
| `provider` | text |
| `model_id` | text |
| `model_version` | text |
| `adapter_version` | text |
| `capabilities` | JSONB — `max_context_tokens`, `supports_parallel_tool_calls`, `cost_per_token` in Phase 0 |
| `known_quirks` | text, nullable |
| `eval_status` | text, nullable — unpopulated until the eval suite exists |
| `production_eligibility` | boolean, default `true` in Phase 0 |

Primary key: `(provider, model_id, model_version)`.

---

## 3. Component-Level Design

Each entry: responsibility, inputs/outputs, and the invariants/edge cases that matter — not implementation, but precise enough that implementation is mechanical.

### 3.1 `ContentProvenance`

```text
origin: system | developer | user | model | internal_tool | external_tool | mcp_resource
instruction_authority: authoritative | advisory | data_only
trust_zone: trusted_source | validated | untrusted
taint_flags: []  # external_content | user_controlled | executable_content | prompt_injection_risk | secret_bearing
source_uri_or_hash: optional
```

**Invariant:** every `ToolResult` carries exactly one `ContentProvenance`. In Phase 0 (no external/MCP tools yet), a local tool's result defaults to `origin=internal_tool`, `instruction_authority=data_only`, `trust_zone=trusted_source`, empty `taint_flags` — the fields exist and are populated correctly even though nothing yet exercises the `untrusted`/tainted path.

### 3.2 `ModelRequest` / `ModelResponse`

```text
ModelRequest: messages, tools, output_schema?, model_settings?, provider_state?, metadata
ModelResponse: message, tool_calls[], structured_output?, stop_reason, usage, provider_response_id?, provider_metadata
```

**Invariant:** `output_schema` is always `None` in Phase 0 — the field exists so `AgentLoop` and `BedrockModelClient` never need to change signature when Phase 2 starts populating it.

### 3.3 `AgentSpec` / `RunConfig` / `Runner` / `RunResult`

```text
AgentSpec: id, name, role, instructions, preferred_model, tool_profile, permission_policy
RunConfig: tenant_id, project_id, max_turns, model_override?, principal_context?
Runner.run(spec, task, config) -> RunResult
RunResult: status, output, events, usage
```

**Invariant:** `Runner.run()` is the only method application code calls. Everything else in this document is an internal collaborator `Runner` composes — no other component is part of the public contract.

### 3.4 `Tool` / `ToolSpec` / `ToolRegistry`

```text
ToolSpec: name, description, input_schema, risk_class, read_only, idempotent, approval_policy
```

**Registration invariant:** registering a `name` that already exists in the registry raises `ToolError` at registration time, not at call time — a duplicate is a startup-time configuration bug, not a runtime condition.

**Lookup invariant:** requesting an unregistered tool name raises `ToolError.NotFound`. This is caught inside `ToolExecutor`'s "resolve tool" step (§3.5), never surfaces as an unhandled exception to `Runner`.

### 3.5 `ToolExecutor`

Exact lifecycle, Phase 0 (only `Completed`/`Failed` outcomes implemented):

1. **Resolve** — look up the tool by name in `ToolRegistry`. Not found → `ToolExecutionOutcome.Failed(ToolError.NotFound)`, lifecycle ends here.
2. **Validate arguments** — check the call's arguments against `ToolSpec.input_schema`. Invalid → `Failed(ToolError.ValidationError)`, ends here. **Never reaches step 3 on a validation failure.**
3. **Permission check** — `PermissionChecker.check(tool_call, principal_context)`. Denied → `Failed(ToolError.PermissionDenied)`, ends here.
4. **Approval** — stubbed to auto-allow in Phase 0 (no `ApprovalManager` yet; the call site exists so Phase 4 doesn't change this lifecycle's shape).
5. **`RuntimeHook.before_tool`** — Phase 0 default: no-op, always returns `continue`.
6. **Execute** — run the tool implementation. Raises → `Failed(ToolError.ExecutionError)`, capturing the exception message.
7. **Assign `ContentProvenance`** to the result (§3.1).
8. **`RuntimeHook.after_tool`** — Phase 0 default: no-op.
9. **Emit `RunEvent(ToolCalled)`**, return `ToolExecutionOutcome.Completed(ToolResult)`.

**Replay-safety rule (documented now, not yet enforced by infrastructure):** Phase 0 does **not** automatically retry a failed tool call. A tool with side effects that fails partway through is surfaced as `Failed` and left for the model to decide whether to retry conceptually — automatic retry is deferred until `ToolSpec.idempotent` and execution-attempt tracking exist (Phase 6).

### 3.6 `PermissionChecker` / `AllowlistPermissionChecker`

```text
check(tool_call, principal_context) -> (decision: ALLOW | DENY, reason: str)
```

**Phase 0 default behavior:** `AllowlistPermissionChecker` ignores `principal_context` entirely and checks only `tool_call.name` against a fixed set — the parameter exists in the signature so Phase 4's principal-aware checkers are a new implementation of the same interface, not a signature change.

### 3.7 `RuntimeHook`

```text
before_model(request) -> continue | modify | halt
after_model(response) -> continue | modify | halt
before_tool(call) -> continue | modify | reject | require_approval | halt
after_tool(result) -> continue | modify | halt
```

**Phase 0 default:** every hook point returns `continue` unconditionally. The interface exists and is wired into `AgentLoop`/`ToolExecutor` now specifically so a real hook implementation in a later phase requires no call-site changes.

### 3.8 `ModelClient` / `BedrockModelClient`

**Translation contract, request direction:** internal `Message` list → Bedrock Messages API format. A `Message` with `role=tool` maps to a `user`-role message containing one `tool_result` content block per `ToolResult`, each carrying `tool_use_id`, `content`, and `is_error`.

**Translation contract, response direction:** each Bedrock response content block maps to either accumulated text (block type `text`) or a `ToolCall` (block type `tool_use`, capturing `id`/`name`/`input`). The assembled result becomes one `ModelResponse` with `stop_reason` taken directly from Bedrock's stop reason field.

**Error mapping:** a Bedrock timeout or throttling response maps to `ModelError.Timeout` / `ModelError.RateLimited` respectively — never surfaces as a raw provider exception past this component's boundary.

### 3.9 `SessionStore` (Postgres-backed)

```text
append(run_id, message) -> None   # INSERT into `messages`, sequence_no = max(sequence_no)+1 for this run_id
history(run_id) -> list[Message]  # SELECT ... WHERE run_id = ? ORDER BY sequence_no
```

**Invariant:** `append` is the only write operation in Phase 0 — no update, no delete. `sequence_no` assignment happens inside `append` under the same transaction as the insert, so ordering is never ambiguous under concurrent appends to the same `run_id` (which shouldn't happen in Phase 0's single-agent model, but the invariant is cheap to hold now and expensive to retrofit once Phase 2 introduces concurrent subagent writes).

### 3.10 `AgentLoop`

Exact algorithm:

1. `sessions.append(run_id, Message(role=user, content=task))`.
2. Loop, up to `max_turns` iterations:
   a. `history = sessions.history(run_id)`.
   b. `request = context_assembler.build(history, tool_registry.schemas())`.
   c. `response = model_client.send(request)`.
   d. `sessions.append(run_id, response.message)`; emit `RunEvent(ModelCalled)`.
   e. If `response.tool_calls` is empty → return `response.message.content` as the final result.
   f. Else, for each tool call, run `ToolExecutor`'s lifecycle (§3.5); collect results.
   g. `sessions.append(run_id, Message(role=tool, tool_results=results))`.
   h. Continue loop.
3. If the loop exits by exhausting `max_turns` without step 2e triggering, return with status `max_turns_exceeded` (`WorkflowError.MaxTurnsExceeded`).

### 3.11 `ContextAssembler` (minimal, Phase 0)

**Phase 0 behavior:** pass the message history straight through into `ModelRequest.messages`, preserving each `ToolResult`'s `ContentProvenance` as request metadata (not as message content the model reads as instructions). No `ContextPolicy` filtering is applied — every message in history goes to the model, since there's exactly one agent and no curated-briefing concept yet.

---

## 4. Sequence Flows

### 4.1 Happy path — single tool-using task completes

1. Application calls `runner.run(spec, "task description", config)`.
2. `Runner` creates a `runs` row (`status=running`), writes `execution_manifests` row, emits `RunEvent(RunStarted)`.
3. `AgentLoop` step 1: task appended to `messages`.
4. `AgentLoop` step 2a–2d: history read, `ModelRequest` built, model called, response appended, `RunEvent(ModelCalled)` emitted.
5. Response contains one tool call → `AgentLoop` step 2f: `ToolExecutor` lifecycle runs (§3.5, steps 1–9), ending in `Completed`.
6. Tool result appended to `messages` (step 2g).
7. Loop continues: next model call returns no tool calls (step 2e triggers).
8. `Runner` updates `runs.status = completed`, emits `RunEvent(RunCompleted)`, returns `RunResult`.

### 4.2 Tool call denied by permission policy

Same as 4.1 through step 5, except `ToolExecutor` step 3 returns `DENY`. Lifecycle ends at step 3 with `Failed(ToolError.PermissionDenied)`. This result is appended to `messages` as a tool result with `is_error=true` — the model sees the denial and can respond accordingly in its next turn. The run does **not** fail outright; a denied tool call is a normal turn outcome, not a run-level error.

### 4.3 Tool execution raises an exception

Same through `ToolExecutor` step 6, which catches the exception and produces `Failed(ToolError.ExecutionError, message=str(exception))`. Same as 4.2 from there — surfaced to the model as an error tool result, not a run failure.

### 4.4 `max_turns` exceeded

`AgentLoop` step 3 triggers after `max_turns` iterations without a turn ending in step 2e. `Runner` sets `runs.status = max_turns_exceeded`, emits `RunEvent(RunFailed)` with `payload.reason = "max_turns_exceeded"`, returns a `RunResult` with that status — this is a defined, expected terminal state, not an exception propagating to application code.

### 4.5 Model provider error

`ModelClient.send()` raises `ModelError.Timeout` or `ModelError.RateLimited`. Phase 0 policy: retry up to 2 times with exponential backoff for these two error classes specifically (transient, safe to retry — no side effects have occurred). Any other `ModelError` subtype propagates immediately without retry. If retries are exhausted, `Runner` sets `runs.status = failed`, emits `RunEvent(RunFailed)`.

---

## 5. Configuration (Phase 0)

| Setting | Purpose |
|---|---|
| Bedrock region + model ID | `BedrockModelClient` construction |
| BYO endpoint URL/key/wire-format | pending ADR-13 |
| Postgres connection string | `SessionStore` and all Data/State tables |
| Default `AllowlistPermissionChecker` tool set | per `AgentSpec`, not global |
| Default `max_turns` | fallback when `RunConfig` doesn't override it |
| Retry count/backoff for `ModelError.Timeout`/`RateLimited` | §4.5 |

---

## 6. Testing Strategy (Phase 0)

**Unit boundaries:** one test suite per §3 component, testing each one against its stated invariants in isolation (e.g., `ToolExecutor` tests exercise all nine lifecycle steps' pass/fail branches without a real model or real Postgres behind them).

**Integration test — the golden task:** a single scenario that exercises the entire Phase-0 exit criteria in one run. Concretely: an agent with one registered tool (`echo`, taking a string and returning it unchanged) is asked to call the tool three times with different inputs, then summarize the three results in one sentence. The scenario deliberately also triggers:
- one permission denial (ask for a tool not in the allowlist, confirm §4.2's path),
- one validation failure (call `echo` with a malformed argument, confirm the validate-arguments step catches it before execution).

**Assertion set**, directly from the v0.3 exit criteria: the run completes; every `ToolResult` carries `ContentProvenance`; the trace reconstructs correctly from `runs` + `messages` + `run_events` together; every row is tenant-scoped; `execution_manifests` has exactly one row for the run with all fields populated.

---

## 7. Traceability Matrix

| LLD component | HLD layer | Governing ADR(s) | v0.3 §13 checklist item |
|---|---|---|---|
| `ContentProvenance` | Control Plane | ADR-17, ADR-26 | ✓ |
| `ModelRequest`/`ModelResponse` | Control Plane | (round-2 P0 correction) | ✓ |
| `ToolExecutor` | Control Plane | ADR-20 | ✓ |
| `RuntimeHook` | Control Plane | (round-2 P0 addition) | ✓ |
| `SessionStore` (Postgres) | Data/State | ADR-05, ADR-11 | ✓ |
| `RunEvent` | Data/State | ADR-21 | ✓ |
| `ExecutionManifest`/`ModelRegistry` | Cross-cutting | ADR-25 | ✓ |
| `PrincipalContext` (metadata only) | Execution/Security | ADR-27 | ✓ |

---

## 8. What This Document Deliberately Does Not Specify

Per §1's scope statement: DAG/`PlanNode` structure, `SchedulerLimits`, `ArtifactRef`/`ArtifactStore` (Phase 2); `EvidenceSourceVersion`, cache scoping, ADR-15's calibration change (Phase 3); `ToolCatalog`, `CredentialBroker`, basic `ApprovalManager` (Phase 4); `WorkspaceManager` (Phase 5); durable `RunInterruption`/replay (Phase 6). Each gets its own LLD when its phase starts — writing it now would mean designing against components (the orchestrator, the evidence ledger) that don't exist yet to validate the design against.
