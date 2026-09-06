# Custom Agent SDK — High-Level Design (HLD)

**Source:** synthesizes `agent-sdk-master-design-v0.3.md` and its ADR register into a standard system-design view.
**Scope:** the full target architecture across all phases. Implementation currently proceeds through Phase 0 — see the Low-Level Design for that phase's implementable detail.

---

## 1. Purpose

Describe the system at the level a new contributor (or a future you, six months from now) needs before touching code: what it is, what it talks to, how it's layered, what each layer is responsible for, and what non-functional properties it's designed to hold.

---

## 2. System Context

**Primary actor:** application code — a Python process that constructs an `AgentSpec` and calls `Runner.run()`. This is the only thing application code ever touches.

**External dependencies:**
- **Model providers** — Bedrock today, a bring-your-own endpoint next (ADR-13, pending wire format).
- **Postgres** — the sole durable backend for every store (ADR-05).
- **MCP servers** — external tool sources, from Phase 4 onward.
- **LangGraph** — optional host. The entire SDK can run as a single node inside a larger LangGraph workflow, but nothing in the SDK depends on LangGraph existing (ADR-01).

**Explicitly not a dependency:** Claude Agent SDK, OpenAI Agents SDK, and Claude Code's Dynamic Workflows informed pattern choices throughout this design, but the system has no runtime dependency on any of them.

```text
[Application code]
      │ constructs AgentSpec, calls Runner.run()
      ▼
   [Runner] ──uses──▶ [ModelClient] ──calls──▶ [Model Provider: Bedrock | BYO]
      │
      ├──uses──▶ [ToolExecutor] ──calls──▶ [Local Tools | MCP Servers (Phase 4+)]
      │
      └──persists to──▶ [Postgres: Sessions, Events, Evidence, Artifacts]

(optional) [LangGraph Node] wraps [Runner] — Runner works identically either way
```

---

## 3. Layered Architecture

Five layers, each depending only on the layers below it:

- **Public API** — `AgentSpec`, `RunConfig`, `Runner`, `RunHandle` (Phase 2+), `RunResult`. The only layer application code ever calls directly.
- **Control Plane** — `AgentLoop`, `ContextAssembler`, `ModelClient`/`ModelRegistry`, `ToolExecutor`, `PolicyEngine`/`PermissionChecker`, `RuntimeHook`. Owns one run's execution sequencing.
- **Orchestration Plane** *(Phase 2+)* — `Orchestrator`, `PlanVersion`, DAG scheduler, `SchedulerLimits`, subagent pool. Owns multi-agent decomposition and scheduling; doesn't exist yet in Phase 0 (single-agent only).
- **Data / State Plane** — `SessionStore` (conversation history), `RunStateStore` (Phase 2+), `EventStore`, `EvidenceStore`/`EvidenceLedger` (Phase 3+), `ArtifactStore` (Phase 2+), `ResourceCache` (Phase 3+), `ModelRegistry`. All backed by one Postgres deployment — separation of *contracts*, not separate infrastructure.
- **Execution, Security & Cross-cutting** — `PrincipalContext`, `DelegationGrant`/`CredentialBroker` (Phase 4+), `WorkspaceManager`/sandbox (Phase 5+), `BudgetGovernor`, error taxonomy, cancellation/deadlines, evaluation harness, telemetry.

**Important nuance the layer diagram simplifies:** Control Plane and Orchestration Plane both call directly into Data/State and Execution/Security — those two bottom layers are shared infrastructure used by everything above, not a strict single-direction pipeline.

---

## 4. Component Catalog

| Component | Layer | Responsibility | Phase |
|---|---|---|---|
| `AgentSpec` | Public API | Declarative definition of an agent: role, instructions, model, tool profile, permission policy | 0 |
| `RunConfig` | Public API | Per-invocation config: tenant/project scope, max turns, model override, principal context | 0 |
| `Runner` | Public API | The single entry point application code calls; owns run lifecycle end to end | 0 |
| `RunResult` | Public API | Terminal output: status, output, events, usage | 0 |
| `RunHandle` | Public API | Live progress/cancellation surface for a streaming run | 2 |
| `AgentLoop` | Control Plane | The send→check-tools→execute→repeat sequencing for one agent | 0 |
| `ContextAssembler` | Control Plane | Builds provider-specific `ModelRequest` from canonical messages, preserves provenance | 0 |
| `ContextPolicy` | Control Plane | Decides what a subagent is allowed/expected to see | 2 |
| `ContextCompactor` | Control Plane | Token-budget summarization when a workload needs it | 7 |
| `ModelClient` | Control Plane | Provider adapter: `send(ModelRequest) -> ModelResponse` | 0 |
| `ModelRegistry` | Control Plane | Known model versions, capabilities, quirks, eval status | 0 |
| `ToolExecutor` | Control Plane | Owns the full tool-call lifecycle; returns `ToolExecutionOutcome` | 0 |
| `ToolRegistry` | Control Plane | Local tool name/schema lookup | 0 |
| `ToolCatalog`/`ToolResolver` | Control Plane | Namespaced, versioned catalog for external (MCP) tools | 4 |
| `PermissionChecker`/`PolicyEngine` | Control Plane | Allow/deny/require-approval decisions | 0 |
| `RuntimeHook` | Control Plane | Intervention points around model/tool calls | 0 |
| `Orchestrator` | Orchestration | Decomposes a task into a plan, spawns subagents, synthesizes results | 2 |
| `PlanVersion` | Orchestration | Immutable DAG version; replanning produces a new version, never an in-place edit | 2 |
| `SchedulerLimits` | Orchestration | Concurrency safety, separate from budget | 2 |
| `SessionStore` | Data/State | Conversation history, tenant-tagged | 0 |
| `RunStateStore` | Data/State | Authoritative execution state (node status, later: replay/idempotency) | 2 (minimal) / 6 (full) |
| `EventStore` | Data/State | Ordered `RunEvent` audit/telemetry stream | 0 |
| `EvidenceStore`/`EvidenceLedger` | Data/State | Structured claims, source dedup, supersede history | 3 |
| `ArtifactStore` | Data/State | Named, hashed artifacts produced by any agent | 2 |
| `ResourceCache` | Data/State | Tenant/auth-scoped raw-content cache tier | 3 |
| `PrincipalContext` | Execution/Security | Who the agent is acting as, distinct from tenant/project scope | 0 (metadata) / 4 (enforced) |
| `CredentialBroker` | Execution/Security | Brokers short-lived, scoped credentials; model never sees raw secrets | 4 |
| `WorkspaceManager`/sandbox | Execution/Security | Isolated execution environment for code/shell tools | 5 |
| `BudgetGovernor` | Cross-cutting | Cost/token ceilings, per-subagent reservation and reclaim | 2 |
| Error taxonomy | Cross-cutting | Stable exception hierarchy | 0 |
| Evaluation harness | Cross-cutting | Capture → Curate → Gate → Promote continuous improvement | 0 (starts) |
| `ExecutionManifest` | Cross-cutting | Per-run version/hash snapshot | 0 (lean) / 8 (enforced) |

---

## 5. High-Level Data Flow

1. Application constructs an `AgentSpec` and `RunConfig`, calls `Runner.run(spec, task, config)`.
2. `Runner` opens a run record in `SessionStore`/`RunStateStore`, tagged with tenant/project/principal, and writes an `ExecutionManifest`.
3. `AgentLoop` begins: `ContextAssembler` builds a `ModelRequest` from history.
4. `ModelClient.send()` calls the provider, returns a `ModelResponse`.
5. If the response has no tool calls, the loop ends and `Runner` returns a `RunResult`.
6. If it has tool calls, each routes through `ToolExecutor`: resolve → validate → permission check (`PolicyEngine`, aware of `PrincipalContext`) → `RuntimeHook.before_tool` → execute → assign `ContentProvenance` → `RuntimeHook.after_tool` → `ToolExecutionOutcome`.
7. Results are appended to `SessionStore`; a `RunEvent` is emitted at every step above.
8. The loop repeats from step 3 until a stop reason or `max_turns`.
9. *(Phase 2+)* If the task warrants decomposition, `Orchestrator` replaces step 3 onward with plan generation, subagent spawning (each subagent running its own instance of this same steps-3–8 loop with a curated context), and synthesis.

---

## 6. Non-Functional Requirements

| Property | How it's achieved |
|---|---|
| Model-agnostic | `ModelClient`/`ModelRequest`/`ModelResponse` canonical contract; capability descriptor per provider |
| Multi-tenant | `tenant_id`/`project_id` mandatory on every record (ADR-11), enforced not just tagged |
| Auditable | `RunEvent` ordered stream + `ExecutionManifest` + `RunStateStore` together reconstruct any run |
| Secure by structure, not by prompting | `ContentProvenance` informs policy; the hard boundary is `PolicyEngine → ToolExecutor → Approval/Credential/Sandbox/Network`, never model behavior (ADR-17) |
| Extensible without vendor lock-in | LangGraph-pluggable, not LangGraph-dependent (ADR-01); provider-neutral model layer |
| Cost-governed | `BudgetGovernor` with per-child reservation and reclaim (ADR-06, pending confirmation); `SchedulerLimits` as a separate concurrency control |
| Deterministic where it matters | DAG-only planning (ADR-02) with immutable versioned replanning (ADR-03) — the plan's shape is always inspectable and never silently mutated |
| Evolvable | Every Phase-0 interface (`ModelRequest`, `ToolExecutionOutcome`, `RunInterruption`) was chosen specifically so later phases add fields/implementations, not replacement contracts |

---

## 7. Deployment View (Phase 0)

- One Python async process hosting `Runner` — either standalone or embedded as a LangGraph node.
- One Postgres database, multiple tables (not separate databases) for Sessions/Events/Manifests/Registry — see LLD §2 for schema.
- Outbound calls only: Bedrock API, and the BYO endpoint once its wire format is known.
- No sandbox, no MCP servers, no message queue, no multi-worker coordination in Phase 0 — a direct in-process call chain, deliberately.

---

## 8. Technology Choices & Rationale

| Choice | Rationale |
|---|---|
| Python, async/await | Matches existing FastAPI/LangGraph stack and prior production experience |
| Postgres | Matches precedent from the triage platform and IAM Audit Intelligence work; one deployment serves every store via logical separation |
| `AsyncAnthropicBedrock` | Existing, proven pattern for the first model provider |
| LangGraph as optional adapter | Preserves the ability to reuse the SDK outside a graph (FastAPI endpoint, CLI, cron) while still composing cleanly inside one |

---

## 9. Phasing Summary

Full detail lives in the master design (§7) and its ADR register (§8). In one line per phase: **0** single-agent skeleton → **2** orchestration + DAG → **3** evidence + critic → **4** MCP + identity + basic approvals → **5** sandbox → **6** durable execution → **7** context compaction → **8** production hardening.

---

## 10. Traceability to ADRs

| HLD section | Governing ADRs |
|---|---|
| §3 Layered architecture | ADR-01, ADR-19–21, ADR-26 |
| §5 Data flow (provenance/security) | ADR-17, ADR-26 |
| §6 Multi-tenancy | ADR-11, ADR-31 |
| §6 Cost governance | ADR-06 (proposed), ADR-10, ADR-30 |
| §6 Determinism | ADR-02, ADR-03 |
| §7 Deployment | ADR-05 |
| §8 Model layer | ADR-13, ADR-25 |
