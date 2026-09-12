# Custom Agent SDK — Master Design Document v0.3

**Supersedes:** `agent-sdk-master-design-v0.2.md`
**Incorporates:** `custom-agent-sdk-round2-claude-refinement-handoff.md`, with one factual claim rejected (see §11) and one recommended ADR resolution kept `Proposed` pending your confirmation rather than silently accepted.

---

## Change Summary — v0.2 → v0.3

| Change | Detail |
|---|---|
| `ModelClient.send(messages, tool_schemas)` → `send(ModelRequest) -> ModelResponse` | Prevents a central API rewrite when Phase 2 needs structured output and richer usage/provider metadata. Phase 0 change, interface only. |
| `ContentProvenance` reframed | **Provenance informs policy; it does not itself enforce policy.** Model-generated content does not automatically clear taint inherited from its inputs. The hard security boundary is `PolicyEngine → ToolExecutor → Approval/Credential/Sandbox/Network`, never "the model respected `data_only`." |
| `ContextAssembler` introduced (Phase 0, minimal) | Owns provider-facing context composition and provenance preservation, distinct from `ContextPolicy` (Phase 2) and `ContextCompactor` (Phase 7). |
| `ToolExecutionOutcome` introduced | Future-proof result type (`Completed`/`InputRequired`/`ApprovalRequired`/`Pending`/`Failed`/`Cancelled`). Phase 0 implements only `Completed`/`Failed`; the type exists now so Phase 4 MCP support doesn't require changing `ToolExecutor`'s central contract. |
| `RunInterruption` introduced (concept only, Phase 0) | One generic pause/resume abstraction for approvals, MCP input-required flows, credential acquisition, and long-running external tasks — approval becomes one interruption *kind*, not its own separate mechanism. |
| **Basic approval moved: Phase 5 → Phase 4** | MCP's input-required/multi-round-trip semantics need *some* interruption mechanism the moment Phase 4 is built — this was an internal contradiction in v0.2 (Phase 4 needed something Phase 5 hadn't built yet). Durable, restart-surviving approval stays Phase 6. |
| `PrincipalContext` introduced (Phase 0, lean metadata) | Distinguishes *who a run belongs to* (tenant/project — already had this) from *who the agent is acting as* (end-user/agent/service identity, delegated authority, scopes) — genuinely the most speculative Phase 0 addition here; see §4.14. |
| `RunEvent` strengthened | Added `schema_version`, `sequence_no`, `agent_id`, `task_id`, `tool_call_id`, `attempt_id`, `parent_event_id`, `correlation_id`. Phase-0 exit criterion corrected: the execution trace is reconstructable from **persisted runtime state plus ordered events together** — events alone were never meant to be the sole source of truth. |
| **`ArtifactRef`/`ArtifactStore` interface moved: Phase 5 → Phase 2** | The Phase-2 subagent result contract already promised "artifacts produced" — this was another internal contradiction (a contract with no defined type). Sandbox-produced artifacts, snapshots, and mounts remain Phase 5. |
| `PlanNode` gains acceptance criteria | A node is `done` only when execution terminated **and** its output contract validates **and** its acceptance criteria are satisfied — not merely "the subagent stopped talking." Phase 2. |
| `SchedulerLimits` introduced, separate from `BudgetGovernor` | Concurrency safety and monetary/token budget are different controls — a run can be affordable but operationally unsafe to fan out unbounded. Phase 2. |
| **ADR-06 — Proposed in v0.3, accepted 2026-09-12** | Run ceiling + per-child reservation + unused-budget reclaim. Recorded as `Proposed` in v0.3; accepted by the owner on 2026-09-12 with soft enforcement in USD and tokens, so the earlier name "hard run ceiling" is dropped. See §4.7. |
| ADR-15 actually applied this time | v0.2's ADR table left ADR-15 unchanged despite round 1 recommending a fix — an oversight, now corrected: adaptive calibration uses externally validated outcomes (deterministic tests, ground-truth fixtures, downstream correctness, human adjudication) where available; critic outcome is one lower-confidence signal, not the label that trains the gating threshold. Phase 3. |
| `EvidenceSourceVersion` introduced | Claims bind to an immutable retrieved version of a source (content hash, retrieval time), not a mutable URL alone — makes the Evidence subsystem reproducible. Phase 3. |
| Evidence cache isolation fixed | A canonical URL is not the same resource across different auth/tenant/locale contexts. Cache scopes (`PUBLIC_GLOBAL`/`TENANT`/`PROJECT`/`SESSION`/`NO_CACHE`) now required for ADR-11 (tenant tagging) to actually mean something inside the Evidence subsystem. Phase 3. |
| `ToolCatalog`/`ToolResolver`/`QualifiedToolName` introduced | Tool identity becomes server-scoped (`server_namespace + tool_name`) so an approval for one server's tool can't silently apply to an identically-named tool on another server. Catalog snapshots detect schema/description changes so a changed tool doesn't inherit an old trust decision. Phase 4. |
| `ModelRegistry` restored, `ExecutionManifest` added | v0.1 had a model registry; v0.2's prose mentioned it without making it a real component. Restored. `ExecutionManifest` is a lean, Phase-0 run-start snapshot (SDK version, spec/instructions/tool hashes, model version) — full compatibility *enforcement* stays Phase 8. |
| Lean `RuntimeHook` introduced (Phase 0) | Separates observation (`RunEvent`, what happened) from intervention (`RuntimeHook`, what's allowed to happen) — `before_model`/`after_model`/`before_tool`/`after_tool`, minimal behavior in Phase 0. |
| **One factual claim rejected** | Round 2 proposed replacing v0.2's Dynamic Workflows date with "July 22, 2026 only." Verified against Anthropic's own blog post (dated June 2, 2026, stating "last week we released dynamic workflows"): the actual launch was **~May 26–28, 2026**. v0.2's original dating was correct; kept as-is. See §11. |

---

## 1. Vision

Unchanged from v0.2: model-agnostic, LangGraph-pluggable but not LangGraph-dependent, Manus AI-inspired orchestrator with isolated subagents. Personal product development (ADR-04).

**Positioning reinforced (v0.3):** Anthropic's own `/deep-research` skill — built on Dynamic Workflows — now does almost exactly what this SDK's Research Analyst use case does: fan out searches, fetch sources, adversarially verify claims, synthesize a cited report. That's further confirmation that the orchestration *pattern* itself isn't a differentiator. What it doesn't do — because it's Claude-specific and not the point of that skill — is maintain a durable, provider-neutral, structurally-deduplicated evidence ledger with supersede history that other systems can build on. That remains this design's differentiator.

---

## 2. Target Use Cases

Unchanged from v0.2.

---

## 3. Public API Model

`AgentSpec`/`RunConfig`/`Runner`/`RunResult` unchanged in shape from v0.2, with one addition:

```text
RunConfig
 ├── tenant_id
 ├── project_id
 ├── max_turns
 ├── model_override
 └── principal_context?     # new — see §4.14
```

`RunHandle` (streaming progress/cancellation surface) is **not** a Phase 0 concern — it arrives in Phase 2 once parallel subagents exist and application code actually needs a live progress surface. Phase 0's `Runner.run()` remains a simple awaitable.

---

## 4. Core Runtime Architecture

### 4.1 Primitives

`Message`, `Role`, `ToolCall`, `ToolResult`, `ContentProvenance` — unchanged shape from v0.2, but the **security framing is corrected**:

> **Provenance informs policy; provenance does not itself enforce policy.**

v0.2's phrasing ("enforced at the message-schema level") overclaimed what a schema field can do — an LLM ultimately receives tokens, and no provider-neutral runtime can assume every provider has a hard technical instruction/data boundary the model cannot cross. A second rule follows from the first:

> **Model-generated content does not automatically clear taint inherited from its inputs.**

Taint propagates: untrusted external content → consumed by the model → a model-generated recommendation → still provenance-linked and tainted → only an explicit validator or policy decision may downgrade or clear it. A model paraphrasing a tainted source does not launder the taint.

The actual, non-negotiable security boundary is structural, not a metadata convention:

```text
PolicyEngine → ToolExecutor → Approval / Credential / Sandbox / Network controls
```

Authorization must never depend on the model correctly respecting `data_only`.

### 4.1b `ContextAssembler` *(new, Phase 0, minimal)*

```text
ContextAssembler
 ├── receives canonical Messages + provenance
 ├── applies ContextPolicy (Phase 2+; no-op in Phase 0)
 ├── preserves instruction/data separation where the provider permits it
 ├── labels/encapsulates untrusted material
 └── produces provider-specific ModelRequest content
```

This is the component that actually sits between "canonical internal messages" and "what a specific provider receives" — v0.2 didn't name it, leaving that translation step implicit inside `ModelClient`. Distinguishing it from `ContextPolicy` (Phase 2 — what a subagent is allowed to see) and `ContextCompactor` (Phase 7 — token-budget summarization) keeps three genuinely different concerns from collapsing into one.

### 4.2 Model Client Layer

**Interface corrected (Phase 0):**

```text
ModelRequest
 ├── messages
 ├── tools
 ├── output_schema?      # unused in Phase 0, but the slot exists
 ├── model_settings?
 ├── provider_state?
 └── metadata

ModelResponse
 ├── message
 ├── tool_calls[]
 ├── structured_output?
 ├── stop_reason
 ├── usage
 ├── provider_response_id?
 └── provider_metadata

ModelClient.send(ModelRequest) -> ModelResponse
```

This is a **small interface change**, not the full future `ModelExecutor`/streaming split — that remains deferred until streaming is an actual requirement. But building `send(messages, tool_schemas)` in Phase 0 and changing it in Phase 2 would mean touching every model adapter and every call site twice; doing it once, now, avoids that.

**Migration impact:** `AgentLoop` builds a `ModelRequest` instead of passing raw messages; `BedrockModelClient` returns `ModelResponse`; `RunEvent.ModelCalled` payloads reference request/response metadata rather than a bare `Message`.

**`ModelRegistry` restored** (was implicit-only in v0.2's prose):

```text
ModelRegistry
 ├── provider, model_id, model_version, adapter_version
 ├── capabilities
 ├── pricing_reference
 ├── known_quirks
 ├── eval_status
 └── production_eligibility
```

**`ExecutionManifest` added** (lean, Phase 0 — full compatibility *enforcement* stays Phase 8):

```text
ExecutionManifest
 ├── sdk_version
 ├── agent_spec_id/version/hash
 ├── instructions_hash
 ├── model_id/version, model_adapter_version
 ├── tool_spec_hashes
 └── policy_version/hash
```

Phase 0 just records these fields per run — it doesn't yet gate anything on them.

### 4.3 Tool Specification & Execution

`ToolSpec`, `ToolRegistry` unchanged from v0.2. `ToolExecutor`'s lifecycle now terminates in a future-proof result type instead of a bare `ToolResult`:

```text
ToolExecutionOutcome
 ├── Completed(ToolResult)
 ├── InputRequired(RunInterruption)
 ├── ApprovalRequired(RunInterruption)
 ├── Pending(ExternalTaskRef)
 ├── Failed(ToolError)
 └── Cancelled
```

**Phase 0 implements only `Completed` and `Failed`.** The type exists now specifically so Phase 4's MCP support — where a tool call can genuinely need more input mid-flight, or kick off a long-running external task — doesn't require changing `ToolExecutor`'s central contract.

### 4.4 Permission / Policy Layer

`PermissionChecker` unchanged. **`RunInterruption` introduced as a concept** (Phase 0: define the type if convenient, no persistence required):

```text
RunInterruption
 ├── interruption_id, run_id, task_id?, tool_call_id?
 ├── kind: tool_approval | additional_user_input | external_tool_input | credential_required | external_task_wait
 ├── request_payload
 ├── created_at
 └── resume_token/state_ref?
```

`ApprovalRequest` becomes one payload shape under `RunInterruption`, not a separate mechanism — this gives one pause/resume abstraction for human approvals, MCP multi-round-trip requests, missing input, and credential acquisition alike.

**Resequenced:** a **basic, in-process, non-durable** `ApprovalManager` now lands in **Phase 4** (not Phase 5) — MCP's input-required semantics need *some* interruption handling the moment MCP exists, and deferring all approval mechanics to Phase 5 would have left Phase 4 with a real gap. **Durable, restart-surviving approval remains Phase 6.**

### 4.4b `RuntimeHook` *(new, lean, Phase 0)*

Observation and intervention are different responsibilities and shouldn't share one abstraction:

```text
RuntimeHook
 ├── before_model(...)
 ├── after_model(...)
 ├── before_tool(...)
 └── after_tool(...)
```

Possible outcomes (`continue`/`modify`/`reject`/`require_approval`/`halt`) are defined now; Phase 0 keeps actual hook behavior minimal. `RunEvent` continues to record *what happened*; `RuntimeHook` is what's allowed to *influence* what happens.

### 4.5 Session & Persistence

Unchanged from v0.2 — Postgres, tenant-tagged from day one. `RunState`'s minimal Phase-2 schema will carry `PlanNode` status including acceptance-criteria results (§4.7); full replay/idempotency semantics remain Phase 6.

### 4.6 Agent Loop

Unchanged in shape; now builds a `ModelRequest` (§4.2) rather than passing raw messages directly.

### 4.7 Orchestrator & Plan Versioning

DAG-only (ADR-02), immutable versioned plans with policy-controlled replanning (ADR-03) — unchanged from v0.2. Two additions, both Phase 2:

**`PlanNode` gains acceptance criteria** — a node isn't `done` just because its subagent stopped generating output:

```text
PlanNode
 ├── node_id, objective, dependencies[], assigned_role
 ├── input_refs[], expected_output_schema?
 ├── acceptance_criteria[]
 ├── budget_reservation, timeout?, retry_policy?, risk_class?
```

A node completes only when: *execution terminated successfully* **and** *output contract is valid* **and** *acceptance criteria are satisfied*. Examples: a research claim needs ≥N independent supporting sources; a test-execution node needs a schema-valid report; a pipeline-build node needs its artifact to exist with a recorded checksum.

**`SchedulerLimits`, separate from `BudgetGovernor`:**

```text
SchedulerLimits
 ├── max_concurrent_subagents, max_concurrent_tools, max_tasks_per_run
 ├── provider_concurrency_limits, tool_concurrency_limits
 └── queue_policy
```

Budget answers "can we afford this"; concurrency limits answer "is it operationally safe to run this many things at once" — a run can be affordable and still unsafe to fan out unbounded.

**ADR-06, accepted 2026-09-12:** run ceiling + per-child reservation + reclaim of unused budget. Inherited/split and a flat cap were rejected; a flat cap would contradict ADR-10's per-subagent governor nested in the per-session one. v0.3 proposed a *hard* run ceiling; enforcement is soft (below), so the name no longer says hard.

```text
Run ceiling:             $10
Orchestrator reserve:     $2
Child A/B/C reservation:  $2 each
Unallocated reserve:      $2

Child A finishes at $0.80 → $1.20 returns to the run pool
```

The figures are illustrative: the reserve sizes are left to the Phase 2 specification.

- **Units.** USD and tokens. A run may set a USD ceiling, a token ceiling, or both; whichever is reached first applies. Reservations and reclaim track every unit that has a ceiling. A USD ceiling on a model with no price is a configuration error raised at the call site; a token ceiling always applies.
- **Enforcement is soft.** Spend is checked before each model call, and an agent already at or over its reservation makes no further call. An agent can therefore exceed its reservation by at most one model call, and a run's committed plus spent budget can exceed its ceiling by at most one model call per concurrently running agent, the orchestrator included. `SchedulerLimits.max_concurrent_subagents` bounds that overshoot. Overshoot is charged to the run; once the run's spend reaches its ceiling, no new model call or child run starts.
- **At the limit.** A child that reaches its reservation ends `failed` with reason `budget_exceeded`, and so does a run that reaches its ceiling. Additional allocation comes only from a deterministic orchestrator policy drawing on the unallocated reserve, never at the model's request.
- **Sizing.** The planner model proposes each `PlanNode`'s `budget_reservation`, and a fixed policy rule caps it. Left to the Phase 2 specification: the reservation cap rule, the fallback when the planner proposes no budget, and the sizes of the orchestrator reserve and the unallocated reserve.
- **Scope.** A nested child reserves from its parent's reservation, never directly from the run pool. Retries of a node draw on that node's reservation, and replanning stays inside the run ceiling. Budgets for plain single-agent runs are not part of this decision. Budgets across a tenant or across runs remain deferred per ADR-10.

Recorded in Genesis as DECISION-e1bf0327, superseding DECISION-f39da722.

### 4.8 Subagent Pool & Isolation

Curated briefing, hub-and-spoke topology, structured result contract, recursion bounded by depth + budget — unchanged from v0.2. **`ArtifactRef`/`ArtifactStore` interface moves here from Phase 5**, since the result contract already promises "artifacts produced" and had no defined type for one:

```text
ArtifactRef
 ├── artifact_id, uri, mime_type, content_hash, size?
 ├── created_by_agent, source_run, source_task
 ├── provenance, classification?

ArtifactStore
 ├── put() / get() / metadata() / delete()/expire()
```

Implementation can be local filesystem or object storage initially. Phase 5 later adds sandbox-produced artifacts, snapshots, and mounts on top of this same identity model — it doesn't redefine it.

`RunHandle` (async event stream, `result()`, `cancel()`, `state()`) and runtime event streaming also land in Phase 2 — once parallel subagents exist, application code needs a live progress/cancellation surface, even with model-level token streaming still deferred.

### 4.9 Evidence Subsystem

Naming and role unchanged from v0.2 (`EvidenceStore`, `EvidenceSource`, `EvidenceClaim`, `EvidenceVersion`, `EvidenceVerification`). Two Phase-3 strengthenings:

**Immutable `EvidenceSourceVersion`** — a URL alone can't prove what a source said at the time a claim was generated:

```text
EvidenceSourceVersion
 ├── source_version_id, canonical_uri, retrieval_time, content_hash
 ├── cache_scope, auth_scope_hash?, freshness_metadata?
 └── prior_version?
```

`EvidenceClaim` now references `supporting_source_versions[]` and `contradicting_source_versions[]` rather than raw URLs — this is what makes the Evidence subsystem actually reproducible.

**Cache isolation fixed** — a canonical URL is not the same resource across different auth/tenant/locale contexts, so the global raw-content cache tier (ADR-16) needs real scoping, or ADR-11's tenant tagging is meaningless once evidence is involved:

```text
ResourceCacheKey
 ├── canonical_resource, auth_scope_hash?, tenant_scope?
 └── request_variant?, content_negotiation_variant?

Cache scopes: PUBLIC_GLOBAL | TENANT | PROJECT | SESSION | NO_CACHE
```

Public unauthenticated content may use global caching; authenticated internal data must not be globally shared across tenants by default.

### 4.10 Confidence Gate & Critic

Rule-based signals first, conditional critic, capped at one pass — unchanged. **ADR-15 corrected (this was recommended in round 1 but never actually applied to v0.2's decision text — an oversight now fixed):**

Old: adaptive threshold learned from historical critic outcomes.
New: adaptive calibration learned from **externally validated outcomes** where available (deterministic test results, known-answer fixtures, downstream correctness, human adjudication); critic outcome is one lower-confidence signal, and cannot on its own train the threshold that decides whether criticism is even required. A conservative, manually configured fallback remains until enough external validation data accumulates. Phase 3.

### 4.11 Error Taxonomy

Unchanged from v0.2.

### 4.12 Runtime Events

**`RunEvent` strengthened:**

```text
RunEvent
 ├── event_id, schema_version, sequence_no
 ├── event_type
 ├── tenant_id, project_id, run_id
 ├── agent_id?, task_id?, tool_call_id?, attempt_id?
 ├── parent_event_id?, correlation_id?
 ├── timestamp
 └── payload
```

**State-authority clarified:** `RunStateStore` (once it exists, Phase 2+) is the authoritative execution state; `RunEvent` is the ordered audit/telemetry stream — not a substitute for it. Phase 0's exit criterion is corrected accordingly: the execution trace is reconstructable from **persisted runtime/session state plus ordered events together**, not from events alone. Full OpenTelemetry export remains Phase 8, but identifiers are chosen now to be compatible with it later.

### 4.13 Context Compaction

Unchanged — deferred to Phase 7, now explicitly named `ContextCompactor` to keep it distinct from `ContextAssembler` (Phase 0) and `ContextPolicy` (Phase 2).

### 4.14 Agent Identity & Delegated Authority *(new)*

**The most speculative addition in this revision** — flagged as such deliberately. `tenant_id`/`project_id` describe a run's ownership and scope, not *who the agent is acting as*. Once (if) any subagent acts against a real enterprise system on someone's behalf, that distinction matters; none of the three current validation use cases clearly require it yet (Research Analyst and Software Tester don't act "on behalf of" anyone in particular; the RAG pipeline build might, once it provisions real infrastructure).

**Phase 0 (lean metadata only):**

```text
PrincipalContext
 ├── user_principal?, agent_principal, service_principal?
 ├── acting_on_behalf_of?, delegation_id?
 └── scopes[]
```

**Deferred to Phase 4** (when real external tool authentication is introduced):

```text
DelegationGrant
 ├── issuer, subject, audience, scopes, expires_at, constraints

CredentialBroker
 ├── resolves tool-specific credentials
 ├── issues/retrieves short-lived credentials
 └── never exposes raw secrets to the model
```

**Security rule:** the model receives capabilities/tool interfaces, never raw bearer tokens or long-lived credentials.

### 4.15 Tool Catalog Integrity *(new, Phase 4, no Phase 0 impact)*

`ToolRegistry` (Phase 0) is fine for a handful of local tools; it's insufficient once MCP introduces large, mutable, externally-controlled catalogs. Phase 4 adds:

```text
ToolCatalog, ToolResolver, ToolCatalogSnapshot
QualifiedToolName = server_namespace + tool_name
```

Tool identity becomes server-scoped so an approval for one server's `lookup_account` tool can never silently apply to an identically-named tool on a different server. Catalog snapshots hash each tool's description/schema — a changed tool doesn't inherit an earlier trust decision. This directly defends against tool poisoning, schema poisoning, and tool "rug-pulls," per OWASP's MCP-specific guidance.

---

## 5. Continuous Improvement Loop

Unchanged in structure. The eventual eval suite (§9 of v0.2) should grow to include tool-catalog-poisoning and cross-tenant evidence-cache-leak scenarios once Phase 4/5 exist to test them — not written speculatively now.

---

## 6. Program Structure

Unchanged from v0.2.

---

## 7. Phased Build Plan *(updated)*

| Phase | v0.3 additions on top of v0.2 |
|---|---|
| **0 — Skeleton** | `ModelRequest`/`ModelResponse`; `ContentProvenance` reframed (informs, doesn't enforce) + taint propagation rule; minimal `ContextAssembler`; `ToolExecutionOutcome` type (`Completed`/`Failed` implemented); `RunInterruption` type (concept only); lean `RuntimeHook`; strengthened `RunEvent`; `PrincipalContext` metadata; restored `ModelRegistry`; lean `ExecutionManifest` |
| **0/1 — Multi-provider** | Proves neutrality via the new `ModelRequest`/`ModelResponse` contract instead of the old positional one; native Anthropic Messages adapter adds explicit prompt-cache markers and returns thinking blocks between turns *(added 2026-09-12)* |
| **M9 — Honest results** *(before Phase 2, added 2026-09-12)* | Scope specified separately by the owner; not yet in `SPEC.md` |
| **M10 — Safe built-in tools** *(before Phase 2, added 2026-09-12)* | Scope specified separately by the owner; not yet in `SPEC.md` |
| **2 — Orchestrator + DAG + replanning** | + `PlanNode` acceptance criteria; `SchedulerLimits` (separate from budget); ADR-06 budget model *(accepted 2026-09-12: run ceiling + per-child reservation + reclaim, soft enforcement in USD and tokens; see §4.7)*; `ArtifactRef`/`ArtifactStore` interface *(moved here from Phase 5)*; `RunHandle` + runtime event streaming; `ContextPolicy`; parallel execution of read-only tool calls under ADR-30 per-run/provider/tool concurrency limits *(added 2026-09-12)* |
| **3 — Evidence + conditional critic** | + `EvidenceSourceVersion` (immutable); tenant/auth-scoped evidence caching; ADR-15 correction actually applied |
| **4 — MCP + identity + basic interruptions** *(expanded)* | Full MCP 2026-07-28 conformance (not just a feature list); `ToolCatalog`/`ToolResolver`/`QualifiedToolName`/catalog snapshots; **basic non-durable `ApprovalManager`** *(moved here from Phase 5)*; `RunInterruption` handling for MCP input-required/MRTR flows; `DelegationGrant`/`CredentialBroker`; tenant-scoped skills and instruction bundles (versioned, stored, hashed into the `ExecutionManifest`, loaded on demand through a tool, never read from the local filesystem); Tier 2 built-in write/edit tools behind the `ApprovalManager` *(added 2026-09-12)* |
| **5 — Sandbox + workspace** | WorkspaceManager/WorkspaceSpec before microVM (unchanged principle); sandbox-produced artifacts/snapshots build on the artifact model from Phase 2; Tier 3 built-in shell and code-execution tools, only inside the sandbox *(added 2026-09-12)* |
| **6 — Sequential/dependent execution + durable approvals** | Full `RunState` replay/idempotency; `RunInterruption` becomes durable/restart-surviving |
| **7 — Context compaction** | `ContextCompactor`, unchanged, only when needed |
| **8 — Production hardening** | Full OTel mapping; full `ExecutionManifest`-based compatibility enforcement; full eval matrix |

---

## 8. Architecture Decisions — Status Summary *(changes only; unlisted ADRs unchanged from v0.2)*

| ADR | Status |
|---|---|
| 06 | **Accepted** 2026-09-12 (was: Proposed in v0.3). Run ceiling + per-child reservation + reclaim of unused budget, in USD and tokens; soft enforcement, with overshoot bounded by one model call per concurrently running agent; inherited/split and flat cap rejected. Phase 2. See §4.7. |
| 15 | **Modified** (v0.2 left this unapplied by oversight). Calibrate from externally validated outcomes; critic is a signal, not the training label. Phase 3. |
| 17 | **Clarified.** Added: "Provenance labels are policy inputs and do not themselves create a hard instruction/data boundary. Taint propagates through model-derived outputs until explicitly cleared by deterministic policy or verification logic." |
| 19 | **Expanded.** Durable run state now explicitly includes generic interruptions, external task references, execution attempts, and resumable approval/input state. |
| 21 | **Expanded.** Events require ordering, schema version, correlation, and causal identifiers from Phase 0; runtime event streaming starts Phase 2 even though token-level model streaming stays deferred. |
| 23 | **Resequenced.** Basic approval/interruption: Phase 4 (was Phase 5). Durable, restart-surviving approval: Phase 6 (unchanged). |
| 24 | **Split.** `ArtifactRef`/`ArtifactStore`: Phase 2 (was Phase 5). `WorkspaceManager`/sandbox/snapshots: Phase 5 (unchanged). |
| 25 | **Clarified.** Minimal `ExecutionManifest` and hashes begin Phase 0; full compatibility *enforcement* stays Phase 8. |
| **26** *(new)* | Context Assembly & Taint Propagation — canonical provenance must survive provider-specific context assembly; model rewriting never automatically clears inherited taint. **Accepted, Phase 0** (this is `ContextAssembler`, §4.1b). |
| **27** *(new)* | Principal Identity & Delegated Authority — every run carries an explicit principal context; credentials are brokered outside model context. **Accepted (lean metadata), Phase 0 / full brokering Phase 4.** Flagged as the most speculative Phase 0 addition — see §4.14. |
| **28** *(new)* | Tool Catalog Identity & Integrity — server-scoped qualified names; versioned catalog snapshots invalidate stale trust decisions. **Deferred, Phase 4.** |
| **29** *(new)* | Interruption Model — pause/resume represented generically via `RunInterruption`; approval is one kind. **Accepted (type only), Phase 0 / basic handling Phase 4 / durable Phase 6.** |
| **30** *(new)* | Scheduler Concurrency & Backpressure — concurrency limits are distinct from budget, enforced per run/provider/tool. **Deferred, Phase 2.** |
| **31** *(new)* | Evidence Cache Isolation & Source Versioning — caching is scoped by visibility/auth/tenant; claims reference immutable source versions. **Deferred, Phase 3.** |

---

## 9. Deferred Capabilities Register *(additions only)*

| Capability | Revisit at |
|---|---|
| `DelegationGrant` / `CredentialBroker` full brokering | Phase 4 |
| `ToolCatalog` scale features (lazy loading, semantic tool search) | Phase 4, only if/when hundreds of MCP tools actually make this necessary |
| Durable `RunInterruption` (restart-surviving) | Phase 6 |
| Full `RuntimeHook` outcome set (`modify`/`reject`/`require_approval`/`halt` behaviors beyond `continue`) | Grows through Phases 2–6 as each has something to intervene on |
| Full `ModelRegistry`-gated model promotion (eval-gated production eligibility) | Phase 8, though the registry itself exists from Phase 0 |
| Parallel execution of read-only tool calls, under ADR-30 per-run/provider/tool concurrency limits | Phase 2 |
| Tenant-scoped skills and instruction bundles: versioned, stored, hashed into the `ExecutionManifest`, loaded on demand through a tool, never read from the local filesystem | Phase 4 |
| Tier 2 built-in write/edit tools, behind the `ApprovalManager` | Phase 4 |
| Tier 3 built-in shell and code-execution tools, only inside the sandbox | Phase 5 |
| Explicit prompt-cache markers; thinking blocks returned between turns | Phase 0/1, native Anthropic Messages adapter |
| Voice and realtime agents | Not planned — non-goal |

Rows from *Parallel execution* down were added 2026-09-12 by the owner's roadmap review against the Claude Agent SDK and the OpenAI Agents SDK, recorded in Genesis as DECISION-f04449c9 (M9 and M10 before Phase 2), DECISION-79f09566 (a response cut off at the output-token limit ends the run as failed, reason max_tokens), DECISION-e6228dd4 (Phase 2 parallel read-only tool calls), DECISION-37bcac5b (Phase 4 skills and Tier 2 tools), DECISION-6a8204d0 (Phase 5 Tier 3 tools), DECISION-67b65b89 (Anthropic Messages adapter), DECISION-09edb52b (voice and realtime non-goal) and DECISION-f39da722 (ADR-06 timing, superseded by DECISION-e1bf0327: ADR-06 accepted).

---

## 10. Security & Provenance Model *(rewritten)*

Two non-negotiable principles now anchor this section:

> **Provenance informs policy; provenance does not itself enforce policy.**
> **Model-generated content does not automatically clear taint inherited from its inputs.**

The enforcement chain is structural, never model-behavioral: `PolicyEngine → ToolExecutor → Approval/Credential/Sandbox/Network`. Concretely, by phase:

- **Phase 0:** every `ToolResult` carries `ContentProvenance`; `ContextAssembler` preserves it into provider-specific requests as far as the provider allows; taint propagates through model outputs rather than being cleared by paraphrase.
- **Phase 4:** MCP-originated metadata is a policy *input*, never absolute truth, unless the MCP server is explicitly trusted; tool catalog snapshots (§4.15) prevent a changed tool from inheriting an old trust decision; `CredentialBroker` keeps raw secrets out of model context entirely.
- **Phase 5/6:** durable approval boundaries and sandbox/network enforcement close the loop — a `prompt_injection_risk`-tainted claim that would trigger a destructive action routes through the (by-then-durable) `ApprovalManager` rather than executing on the model's say-so.

Sanitization/filtering and domain allowlisting remain defense-in-depth, never the sole line of defense, exactly as originally decided in ADR-17.

---

## 11. Positioning vs. Claude Agent SDK / OpenAI Agents SDK *(one correction, one reinforcement)*

**Correction:** the round-2 review recommended replacing v0.2's dating of Dynamic Workflows with "publicly documented July 22, 2026" only, on the grounds that the earlier window was unverified. I checked directly: Anthropic's own blog post (`claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code`) is dated **June 2, 2026** and states "last week, we released dynamic workflows in Claude Code" — placing the actual launch at **~May 26–28, 2026**. This is a first-party, dated, primary source. July 22, 2026 appears to be when a separate SDK-oriented cookbook notebook was published — a later, narrower artifact, not the feature's launch date. **v0.2's original dating was correct and is kept.**

**Reinforcement:** Anthropic's own `/deep-research` skill, built on Dynamic Workflows, now performs essentially the same fan-out/fetch/adversarially-verify/synthesize pattern this SDK's Research Analyst use case targets. Positioning stays as stated in v0.2 — evidence-centric coordination and provider neutrality, not orchestration sophistication, is the differentiator — and this is now doubly confirmed rather than newly threatened by it.

---

## 12. Open Items Requiring Your Input

1. **ADR-06** — accepted 2026-09-12 (§4.7). Left open for the Phase 2 specification: the reservation cap rule, the fallback when the planner proposes no budget, and the sizes of the orchestrator reserve and the unallocated reserve.
2. **ADR-13** — wire format for the bring-your-own model endpoint.
3. **ADR-18** — not blocking; revisit when Track B starts.
4. **ADR-27 (`PrincipalContext`)** — worth a quick gut-check: none of the three validation use cases clearly need delegated-authority semantics yet. Fine to keep as lean, cheap, unused metadata for now, but flagging in case you'd rather cut it until a use case actually calls for it.

---

## 13. Phase 0 Implementation Checklist *(supersedes v0.2 §13)*

- [ ] `Message`, `Role`, `ToolCall`, `ToolResult` — with `ContentProvenance` on every `ToolResult`, taint-propagation rule documented in code comments at the point where model output is generated from tainted input
- [ ] `ModelRequest` / `ModelResponse` — `ModelClient.send(ModelRequest) -> ModelResponse`
- [ ] `ContextAssembler` — minimal, no `ContextPolicy` logic yet
- [ ] `ToolSpec` (lean), `ToolRegistry` (lookup only)
- [ ] `ToolExecutor` — full lifecycle, returning `ToolExecutionOutcome` (`Completed`/`Failed` only)
- [ ] `RunInterruption` type defined (no persistence)
- [ ] `PermissionChecker` protocol + `AllowlistPermissionChecker`
- [ ] Lean `RuntimeHook` interface (behavior minimal)
- [ ] `ModelClient` protocol + `BedrockModelClient`; `ModelRegistry`; lean `ExecutionManifest`
- [ ] `SessionStore` protocol + Postgres-backed implementation, tenant/project_id on every record
- [ ] `PrincipalContext` metadata on `RunConfig` (unused beyond storage in Phase 0)
- [ ] `AgentLoop` — builds `ModelRequest`, routes tool calls through `ToolExecutor`
- [ ] `AgentSpec`, `RunConfig`, `Runner`, `RunResult`
- [ ] Error taxonomy
- [ ] Strengthened `RunEvent` envelope (§4.12) + emission at the Phase-0 event set
- [ ] Golden eval: one representative multi-step tool-using task
- [ ] **Exit signal:** the task completes reliably; every tool result carries provenance; the execution trace reconstructs from persisted session/runtime state **plus** ordered events (not events alone); every record is tenant-scoped; no subagent, MCP, sandbox, or durable approval exists yet

---

## 14. Minimum Public API Sketch

```text
spec = AgentSpec(
    id="research-analyst-v1",
    role="research",
    instructions="...",
    preferred_model="bedrock:claude-sonnet",
    tool_profile=["web_search"],
    permission_policy=AllowlistPermissionChecker({"web_search"}),
)

config = RunConfig(
    tenant_id="default",
    project_id="phase0-validation",
    max_turns=10,
    principal_context=PrincipalContext(agent_principal="research-analyst-v1"),
)

runner = Runner(model_clients={"bedrock": BedrockModelClient(...)}, session_store=PostgresSessionStore(...))

result: RunResult = await runner.run(spec, task="...", config=config)

result.status    # completed | failed | max_turns_exceeded
result.output
result.events    # RunEvent stream, now with sequence_no/correlation_id
result.usage
```

Internally, `AgentLoop` now builds a `ModelRequest` and calls `ModelClient.send(request) -> ModelResponse`; tool calls route through `ToolExecutor.execute(...) -> ToolExecutionOutcome`. None of this is visible to application code — that visibility boundary is the entire point of `Runner`.

---

## 15. Next Step

Resolve §12's four items (ADR-06, since accepted on 2026-09-12, is the only one that changes Phase 2 code; the others are either non-blocking or a one-line answer), then build Phase 0 against §13. Every Phase-0 interface introduced in v0.2 and v0.3 alike was chosen specifically so that Phases 2 through 6 add implementations and fields, not replacement contracts.
