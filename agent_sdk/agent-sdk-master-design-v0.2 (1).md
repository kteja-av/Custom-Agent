# Custom Agent SDK — Master Design Document v0.2

**Supersedes:** `agent-sdk-master-design.md` (v0.1)
**Incorporates:** `custom-agent-sdk-claude-enhancement-handoff.md`, triaged into (a) adopted into Phase 0, (b) sequenced into the phase that already needed it, (c) one confirmed ADR reversal, (d) deferred capabilities recorded but not built yet.

---

## Change Summary — v0.1 → v0.2

| Change | Detail |
|---|---|
| **ADR-03 reversed** (confirmed) | Fixed-upfront plan → immutable versioned plans + policy-controlled replanning. Full old/new/rationale/migration record in §8. |
| **ADR-02 promoted** | DAG-only, no classifier → now **Accepted** (independently confirmed by the enhancement review; two separate analyses converged, no objection raised). |
| `ContentProvenance` replaces boolean `trusted` field | Origin / instruction-authority / trust-zone / taint model. Adopted into Phase 0 — schema-level, cheap now, expensive to retrofit. |
| `ToolExecutor` introduced | Dedicated tool-call lifecycle owner, separate from `ToolRegistry`. Adopted into Phase 0 as a natural extension of the existing single-responsibility split. |
| `AgentSpec` / `RunConfig` / `Runner` introduced | A stable public API boundary around `AgentLoop`. Adopted into Phase 0 in lean form — only the fields Phase 0 needs; the schema grows later rather than being fully speculated now. |
| Error taxonomy introduced | Stable exception hierarchy. Adopted into Phase 0. |
| `RunEvent` canonical envelope introduced | Basic shape adopted into Phase 0 for debugging/audit; the full 20-event lifecycle taxonomy matures through later phases as those subsystems (subagents, critic, MCP) come online. |
| "Shared scratchpad" renamed | Now the **Evidence subsystem** (`EvidenceLedger`, `EvidenceStore`, `EvidenceClaim`, `EvidenceSource`, `EvidenceVersion`, `EvidenceVerification`) — naming only, no behavior change. Implementation still lands in Phase 3. |
| ADR-17 rewritten | From "structural separation + sanitization + allowlist" to a provenance/taint/least-privilege/durable-approval/sandbox model, grounded in `ContentProvenance` rather than a boolean. |
| **New ADRs 19–25 recorded** | Durable run state, tool execution lifecycle, event/streaming model, observability, durable approvals, workspace/sandbox/artifact lifecycle, version pinning. Each assigned to the phase that already needed it — not pulled forward into Phase 0 wholesale. |
| Positioning updated | Reflects verified facts: Claude Code's Dynamic Workflows (deterministic script-based multi-agent orchestration, shipped ~May–June 2026), the MCP 2026-07-28 stateless spec, and OpenAI Agents SDK's `RunState`/sandbox model. Orchestration sophistication alone is no longer claimed as unique. |
| Phase 0 scope | Deliberately kept lean — **5 additions** on top of v0.1's skeleton, not the enhancement review's full 19-item list. Everything else sequenced into the phase that already needed it (see §7). |

---

## 1. Vision

A model-agnostic agent framework, built in-house rather than adopted from a vendor:

- **Model-agnostic** — wireable to any LLM provider (Bedrock today, a bring-your-own endpoint next).
- **LangGraph-pluggable** — usable as a node inside a larger LangGraph workflow, but not dependent on LangGraph to function (ADR-01).
- **Manus AI-inspired** — a meta-agent (the orchestrator) that plans a task, spawns multiple subagents each with an isolated context, and synthesizes their results.

Built as personal product development (ADR-04), not tied to an NS/PwC engagement or formal review process at this stage.

**Updated positioning (v0.2):** sophisticated multi-agent orchestration is no longer a differentiator on its own — Claude Code's Dynamic Workflows and the OpenAI Agents SDK both demonstrate mature orchestration now. This SDK's durable differentiators are: provider-neutral runtime, an evidence-centric architecture where subagents contribute structured, provenance-tagged claims rather than raw text, structural source deduplication, explicit correction history via supersede pointers, cost-aware conditional verification, one DAG model for both parallel and sequential work, and governance via a continuous evaluation gate.

---

## 2. Target Use Cases

| Use case | Shape | New capability it proves |
|---|---|---|
| **Deep Research Analyst** (e.g. market analysis) | Parallel fan-out, read-only | Subagent isolation, evidence dedup, synthesizer, MCP-sourced browser/search tools |
| **Software Tester** (codebase QA) | Mixed parallel + sandboxed execution | Sandboxed tool execution, critic/verifier pattern, permission gating for semi-risky actions |
| **Agentic RAG pipeline build** | Strict sequential DAG | Dependency-aware planning, heaviest permission surface, durable approvals before destructive steps |

All three reduce to the same skeleton — plan, spawn specialists with a scoped tool profile, isolate context, collect structured results, synthesize.

---

## 3. Public API Model *(new in v0.2)*

**Fact vs. recommendation:** the v0.1 design had no stable developer-facing contract — every use of the runtime would otherwise hand-assemble `AgentLoop`, a model client, a registry, a permission checker, and a session store. The enhancement review's `AgentSpec`/`Runner` pattern (matching both vendor SDKs) closes that gap. Adopted in **lean form** — only the fields Phase 0 actually exercises; the schema is expected to grow, not be fully specified today.

```text
AgentSpec
 ├── id
 ├── name
 ├── role
 ├── instructions
 ├── preferred_model
 ├── tool_profile
 └── permission_policy
      # deferred fields, added when the phase that needs them arrives:
      # output_schema (Phase 2, structured subagent results)
      # budget_policy (Phase 2, recursion/budget model)
      # context_policy (Phase 3, curated-briefing rules)
      # hooks (Phase 0 basic version, full taxonomy later)

RunConfig
 ├── tenant_id
 ├── project_id
 ├── max_turns
 └── model_override

Runner
 ├── run(agent_spec, task, run_config) -> RunResult
 └── (owns: session lifecycle, event emission, error translation)

RunResult
 ├── status
 ├── output
 ├── events            # RunEvent stream, see §4.12
 └── usage             # tokens/cost, minimal in Phase 0
```

`Runner` is the only thing an application ever calls directly. `AgentLoop` (§4.6) becomes an internal collaborator `Runner` composes, not something calling code assembles by hand.

---

## 4. Core Runtime Architecture

### 4.1 Primitives

`Message`, `Role` (user/assistant/tool), `ToolCall`, `ToolResult` — a canonical internal shape every model provider translates to and from.

**`ContentProvenance` replaces the boolean `trusted` field** *(critical correction from the enhancement review — adopted as-is)*. A boolean is wrong because model-generated content is not automatically trustworthy — a model can take a dangerous action because it consumed untrusted context, and trust must not "wash clean" just because it passed through the model.

```text
ContentProvenance
 ├── origin              # system | developer | user | model | internal_tool | external_tool | mcp_resource
 ├── instruction_authority   # authoritative | advisory | data_only
 ├── trust_zone           # trusted_source | validated | untrusted
 ├── taint_flags[]        # external_content | user_controlled | executable_content | prompt_injection_risk | secret_bearing
 └── source_uri_or_hash
```

Every `ToolResult` carries one. This is a schema-level decision made cheap by building it in Phase 0, before any tool exists that would make retrofitting it expensive.

### 4.2 Model Client Layer (model-agnostic)

- `ModelClient` protocol — `send(messages, tool_schemas) -> Message`, unchanged from v0.1 for Phase 0.
- **Deferred capability:** the enhancement review's full `ModelExecutor`/streaming/`ModelEvent` split (§6 of the review) is the right eventual shape, but it's not required to prove model-agnosticism against two providers. Adopt the richer capability descriptor (`supports_streaming`, `supports_structured_output`, etc.) incrementally, as each capability actually gets exercised, rather than specifying all ten fields before any of them are used.
- Capability descriptor (Phase 0/1): `max_context_tokens`, `supports_parallel_tool_calls`, `cost_per_token` — the three that actually gate a Phase 0/1 decision.
- Second provider (ADR-13): bring-your-own endpoint. **Still blocked on wire format.**

### 4.3 Tool Specification & Execution

- `ToolSpec` (lean version — the enhancement review's 16-field version is a target, not a Phase 0 requirement):

```text
ToolSpec
 ├── name
 ├── description
 ├── input_schema
 ├── risk_class        # e.g. read_only | write | destructive
 ├── read_only
 ├── idempotent
 └── approval_policy   # stubbed to "auto" until ApprovalManager exists (Phase 5/6)
```

- `ToolRegistry` — name/schema lookup only, unchanged from v0.1.
- **`ToolExecutor` introduced** *(new in v0.2, ADR-20)* — owns the full tool-call lifecycle as a single component, separate from both `ToolRegistry` (lookup) and `PermissionChecker` (the allow/deny decision itself):

```text
resolve tool → validate arguments → permission check → 
[approval — stubbed to auto-allow until Phase 5/6] →
execute → assign ContentProvenance to the result → 
audit/event emission → return ToolResult
```

This is a natural extension of the single-responsibility split already in place, not new complexity — it just gives the lifecycle an explicit owner instead of leaving it implicit inside `AgentLoop`.

### 4.4 Permission / Policy Layer

- `PermissionChecker` protocol, unchanged from v0.1. Default: `AllowlistPermissionChecker`.
- Spawning a subagent is gated through this same layer (unchanged).
- **Deferred capability (ADR-23):** the enhancement review's `ApprovalManager` with `ALLOW / DENY / REQUIRE_APPROVAL / ALLOW_WITH_MODIFIED_INPUT` and durable, restart-surviving approval requests is the right design — for **Phase 5/6**, once real destructive actions and HITL gates exist. Building it now, before any tool needs it, would be effort spent on a problem Phase 0 doesn't have.

### 4.5 Session & Persistence

- `SessionStore` protocol, backend **Postgres** (ADR-05), tenant-tagged from day one (ADR-11) — unchanged from v0.1.
- **Deferred capability (ADR-19):** the enhancement review correctly distinguishes conversation state ("what did participants say") from durable run state ("what has executed, committed, paused, failed, resumed"). A full `RunStateStore` with checkpointing, idempotency keys, and side-effect commit tracking matters once **Phase 6** introduces destructive, resumable pipeline work — not before. Phase 2 introduces a minimal `RunState` schema (run status, DAG node status) because the DAG executor needs *something* to track node completion; full replay-safety semantics mature in Phase 6.

### 4.6 Agent Loop

Unchanged from v0.1: send history + tool schemas → model → no tool calls, return; else check permission (now: routed through `ToolExecutor`) → execute or deny → append results → repeat until done or `max_turns`. Now composed by `Runner` (§3) rather than assembled directly by calling code.

### 4.7 Orchestrator & Plan Versioning *(updated — ADR-03 reversed)*

- Decomposes a task into a **plan**, spawns subagents, collects and synthesizes results.
- **Plan representation: DAG, from the start** (ADR-02, now Accepted) — a flat list is a degenerate DAG (zero edges), so one representation and one executor covers both parallel and sequential work.
- **Plan mutability — reversed in v0.2:** ~~fixed upfront~~ → **immutable versioned plans with policy-controlled replanning.**

  Plan v1 is generated at the start and never edited in place. A subagent's finding can trigger a `ReplanRequest`; if it clears policy (budget remaining, replan count not exhausted, risk level acceptable), it produces Plan v2 — a new immutable object recording what it preserved from v1, what changed, and why. Both versions remain permanently queryable.

  ```text
  Plan v1
     ↓ unexpected evidence / failed assumption
  ReplanRequest
     ↓ policy / budget / recursion check
  Plan v2
   ├── parent_plan = v1
   ├── reason
   ├── preserved_completed_nodes
   ├── modified_nodes
   └── new_nodes
  ```

  **Non-negotiable rule:** completed side-effecting nodes are never silently rerun by a replan.

  **This lands in Phase 2**, not Phase 0 — Phase 0 has no orchestrator or plan at all (single agent, no spawning). The DAG executor needs a `parent_plan` pointer field and a `ReplanRequest` policy check from the moment it's built in Phase 2, since retrofitting versioning into an already-built single-version executor would mean rebuilding its core loop.

- The plan is a **visible, persisted object** (status per subtask), both a progress-tracking surface and a discipline that forces real decomposition.

### 4.8 Subagent Pool & Isolation

Unchanged from v0.1: curated briefing (goal + relevant facts + scoped tool profile) rather than the full parent transcript; hub-and-spoke topology only; structured result contract; recursion bounded by depth + inherited budget (ADR-06 — still open, see §12).

### 4.9 Evidence Subsystem *(renamed from "shared scratchpad")*

Naming-only change, adopted because this is the design's strongest differentiator and deserves to be named as a first-class subsystem rather than described generically. No behavior change from v0.1; still built in **Phase 3**.

```text
EvidenceStore        # was: source cache — keyed by normalized URL/resource hash, cache-aside at the tool layer
EvidenceSource        # a fetched/cited source
EvidenceClaim         # was: fact ledger entry — a claim tagged with source, contributing subagent, timestamp, confidence
EvidenceVersion        # supersede-pointer chain (ADR-07)
EvidenceVerification    # was: critic outcome — approved / corrected / escalate
```

Claim-lock/singleflight dedup, retry-on-failure with required backoff (ADR-08), session-scoped ledger plus a global raw-content cache tier (ADR-16) — all unchanged from v0.1.

### 4.10 Confidence Gate & Critic

Unchanged from v0.1: rule-based signals first (no LLM call for a clean case), critic spawned conditionally, capped at one pass, adaptive threshold with a conservative fallback (ADR-15).

### 4.11 Error Taxonomy *(new in v0.2)*

Adopted into Phase 0 — cheap, and prevents ad hoc exception handling from sprawling once `ToolExecutor` and the orchestrator both exist.

```text
AgentSDKError
 ├── ModelError (Timeout, RateLimited, ProviderUnavailable, InvalidStructuredOutput)
 ├── ToolError (NotFound, ValidationError, PermissionDenied, ApprovalRequired, Timeout, ExecutionError)
 └── WorkflowError (BudgetExceeded, MaxTurnsExceeded, MaxDepthExceeded, DependencyFailed, ReplanLimitExceeded, Cancelled)
```

**Replay-safety rule (applies from Phase 2 onward, once retries exist near side effects):** never automatically retry an operation with uncertain completion if it may have produced a non-idempotent side effect. Use `ToolSpec.idempotent` and an execution-attempt ID to decide.

### 4.12 Runtime Events *(new in v0.2, ADR-21)*

Basic canonical envelope adopted into Phase 0 — pays for itself immediately in debugging even before subagents, MCP, or a critic exist to emit richer event types.

```text
RunEvent
 ├── event_id
 ├── event_type
 ├── tenant_id
 ├── run_id
 ├── task_id            # populated once Phase 2 introduces plan nodes
 ├── timestamp
 └── payload
```

Phase 0 emits a small set (`RunStarted`, `ModelCalled`, `ToolCalled`, `RunCompleted`, `RunFailed`). The full lifecycle taxonomy (`SubagentSpawned`, `EvidenceAdded`, `CriticRequested`, etc.) grows as each subsystem is built — not specified in full today.

### 4.13 Context Compaction

Unchanged from v0.1 — deferred until a specific subagent needs it (Phase 7).

---

## 5. Continuous Improvement Loop

**Capture → Curate → Gate → Promote**, unchanged in structure from v0.1.

**Deferred capability:** the enhancement review's full evaluation taxonomy (functional / reliability / security / concurrency / model-compatibility / quality / economics — ~60 scenarios) is the right eventual target, not a Phase 0 requirement. The golden suite grows organically through this loop as real failures and new phases surface new categories to test, rather than being written wholesale upfront. Security-category tests (indirect prompt injection, malicious MCP tool descriptions, sandbox escape attempts) become meaningful once Phase 4/5 introduce MCP and sandboxing — they're recorded as a target category now, populated later.

The recurring side-by-side comparison against raw Claude Agent SDK (ADR-12) continues as before, run after every major phase.

---

## 6. Program Structure

Unchanged from v0.1 — Track A (core harness, priority 1), Track B (data substrate, parallel/independent, ADR-18 still open), Track A → Research Analyst → Software Tester, Bridge (Agentic RAG, needs A + B mature).

---

## 7. Phased Build Plan *(updated)*

| Phase | Goal | New in v0.2 | Validates against |
|---|---|---|---|
| **0 — Skeleton** | Core loop end to end | + `ContentProvenance`, `ToolExecutor`, lean `AgentSpec`/`RunConfig`/`Runner`, error taxonomy, basic `RunEvent` | Single multi-step tool-using task |
| **0/1 — Multi-provider** | `ModelClient` genuinely generalizes | unchanged | Bedrock + BYO endpoint — blocked on wire format |
| **2 — Orchestrator + DAG + replanning** | Context isolation, fan-out/fan-in, **plan versioning built in from the start** | + `Plan`/`PlanVersion`, `ReplanRequest` policy check, minimal `RunState` (node status only), basic cancellation token | Research Analyst |
| **3 — Evidence substrate + conditional critic** | Dedup and confidence gate hold up under contention | Renamed to Evidence subsystem (naming only) | Research Analyst, overlapping subagents |
| **4 — MCP integration** | Tools from an external server, scoped per role | Full MCP 2026-07-28 compliance (stateless core, auth hardening, tool-list caching) lands here, not earlier | Real MCP browser/search server |
| **5 — Sandbox + recursion controls + workspace abstraction** | Write/execute capability safe before it's needed | `WorkspaceManager`/`WorkspaceSpec` defined **before** the microVM provider is wired in (abstraction first, per the enhancement review); `ApprovalManager` (basic, non-durable) introduced here | Software Tester |
| **6 — Sequential/dependent execution + durable approvals** | Prove the DAG executor against real dependencies and destructive writes | Full `RunState` replay/idempotency semantics; `ApprovalManager` becomes durable (survives restarts, resumes the original run) | Agentic RAG pipeline build |
| **7 — Context compaction** | Only once needed | unchanged | Whichever subagent first needs it |
| **8 — Production hardening** | Runnable as a real product | Full OpenTelemetry mapping; version pinning (ADR-25) across models/tools/agent definitions; full eval matrix | All three use cases end to end |

---

## 8. Architecture Decisions — Status Summary *(updated)*

| ADR | Decision | Status |
|---|---|---|
| 01 | Subagent spawning: framework-first | Accepted |
| 02 | Plan representation: DAG-only | **Accepted** *(promoted — independently confirmed by the enhancement review)* |
| **03** | **Plan mutability** | **Changed.** Old: fixed upfront. New: immutable versioned plans + policy-controlled replanning (§4.7). Rationale: a fixed plan forces mid-run discoveries outside the formal system (manual, unlinked second run); versioning keeps the response inside the same run, budget, and audit trail, at the cost of the plan no longer being a pure function of the input alone. Migration impact: the Phase 2 DAG executor must be built with `parent_plan` versioning from the start. **Status: Accepted (confirmed by you).** |
| 04 | Project identity: personal product | Accepted |
| 05 | Session/audit store: Postgres | Accepted |
| 06 | Recursion budget: inherited/split | Proposed — checkbox vs. rationale mismatch, still unconfirmed. *(The enhancement review offers a candidate resolution: hard run ceiling + per-child reservation + reclaim unused budget — available if you want it, not adopted without your confirmation.)* |
| 07 | Ledger versioning: supersede pointers | Accepted |
| 08 | Claim-lock: retry-on-failure + backoff | Accepted |
| 09 | Sandbox: microVM per session | Accepted, **now sequenced behind a `WorkspaceManager` abstraction (Phase 5)** rather than implemented directly |
| 10 | Cost governor: per-subagent nested in per-session | Accepted |
| 11 | Multi-tenancy: tag every record | Accepted |
| 12 | Model-harness gap: test after every phase | Accepted |
| 13 | Second provider: BYO endpoint | Proposed — needs wire format |
| 14 | Tool-profile assignment: ad hoc per spawn | Accepted |
| 15 | Confidence threshold: adaptive | Accepted |
| 16 | Dedup scope: session + global cache tier | Accepted |
| 17 | **Prompt injection defense** | **Rewritten.** Old: structural separation + sanitization + allowlist. New: provenance-aware policy enforcement + instruction/data separation + least privilege + taint propagation + durable approval boundaries + sandbox/network enforcement; sanitization is defense-in-depth only. Grounded in `ContentProvenance` (§4.1, Phase 0) rather than a boolean. Durable-approval and sandbox/network pieces land in Phase 5/6. |
| 18 | RAG substrate relationship | Open |
| **19** *(new)* | Durable run state & replay semantics | Deferred capability — decision direction recorded (§4.5), implementation in **Phase 6** |
| **20** *(new)* | Tool execution lifecycle | **Accepted, Phase 0** — this is `ToolExecutor` (§4.3), being built now |
| **21** *(new)* | Unified event/streaming model | **Accepted (basic), Phase 0** — full taxonomy matures through Phase 8 |
| **22** *(new)* | Observability / OpenTelemetry mapping | Deferred capability — **Phase 8** |
| **23** *(new)* | Durable human approval model | Deferred capability — **Phase 5 (basic) / Phase 6 (durable)** |
| **24** *(new)* | Workspace, sandbox, snapshot, artifact lifecycle | Deferred capability — abstraction defined **Phase 5**, before the microVM provider |
| **25** *(new)* | Version pinning & compatibility | Deferred capability — minimal fields (model/tool version strings) from Phase 0, full compatibility checking **Phase 8** |

---

## 9. Deferred Capabilities Register

Recorded explicitly so nothing here gets silently forgotten — these are *intentional* non-decisions for now, not oversights:

| Capability | Why deferred | Revisit at |
|---|---|---|
| Full `ModelExecutor` streaming/event split | Not needed to prove model-agnosticism against two providers | When streaming UI becomes a requirement |
| Durable `RunState` with full replay/idempotency | No destructive, resumable work exists yet to need it | Phase 6 |
| `ApprovalManager` durability across restarts | No HITL gate exists yet that would need to survive one | Phase 5 (basic) / Phase 6 (durable) |
| Full MCP 2026-07-28 protocol compliance | No MCP integration exists yet | Phase 4 |
| `WorkspaceManager`/sandbox contract, microVM provider | No sandboxed execution exists yet | Phase 5 |
| Full OpenTelemetry GenAI semantic mapping | No observability backend exists yet to send it to | Phase 8 |
| Full 60-scenario evaluation matrix | Categories only meaningful once their subsystems exist | Grows continuously via §5's loop |
| Full version-pinning/compatibility system | No paused/resumable run exists yet that a deployment could break | Phase 8 |

---

## 10. Security & Provenance Model

Grounded in `ContentProvenance` (§4.1), built in Phase 0. The full ADR-17 policy (see §8) layers on top as its dependent pieces come online:

- **Phase 0 (now):** every `ToolResult` carries origin, instruction-authority, trust-zone, and taint flags. The model layer treats fetched content as `data_only` by construction — never as instructions — enforced at the message-schema level, not by prompting.
- **Phase 4/5 (when relevant):** MCP-originated metadata is treated as policy *input*, not absolute truth, unless the MCP server itself is explicitly trusted.
- **Phase 5/6 (when relevant):** durable approval boundaries and sandbox/network enforcement close the loop — a `prompt_injection_risk`-tainted claim that would trigger a destructive tool call routes through the (by-then-durable) `ApprovalManager` rather than executing automatically.

Sanitization/filtering and domain allowlisting remain in place as defense-in-depth, exactly as decided in the original ADR-17 — never the sole line of defense.

---

## 11. Positioning vs. Claude Agent SDK / OpenAI Agents SDK *(updated)*

Verified as of this writing:

- **Claude Code's Dynamic Workflows** (shipped ~end of May/June 2026): Claude writes a deterministic script that coordinates a subagent fleet with isolated contexts and workflow-level fact-checking. This is primarily a Claude Code capability, with SDK-based examples showing the same pattern is buildable on the Agent SDK. Multi-agent orchestration sophistication is therefore **not claimed as unique** in v0.2.
- **MCP 2026-07-28**: a real, major protocol shift to a stateless core, header-based routing, and hardened authorization. §7 Phase 4 targets this spec directly rather than the older session-based model.
- **OpenAI Agents SDK**: `RunState` as a durable, serializable pause/resume boundary with interruption-based human-in-the-loop, and sandbox agents with three persistence tiers. This SDK's `RunState`/`ApprovalManager` design (§4.5, §4.4) is directly informed by this pattern, sequenced into Phase 6 rather than Phase 0.

**Revised product statement:** a provider-neutral agent runtime for durable multi-agent workflows where evidence, provenance, permissions, verification, and execution state are first-class, inspectable architectural objects — differentiated by evidence-centric coordination and governance, not by orchestration sophistication alone.

---

## 12. Open Items Requiring Your Input

1. **ADR-06** — confirm inherited/split budget as originally checked, or take the enhancement review's candidate resolution (hard run ceiling + per-child reservation + reclaim unused budget), or switch to a flat cap.
2. **ADR-13** — wire format (OpenAI-compatible / Anthropic-compatible / custom) for the bring-your-own model endpoint.
3. **ADR-18** — not blocking; revisit when Track B (data substrate) starts.

(ADR-02 and ADR-03, previously open, are now resolved — see §8.)

---

## 13. Phase 0 Implementation Checklist

What Phase 0 must contain when done — the original v0.1 skeleton plus the five v0.2 additions, nothing more:

- [ ] `Message`, `Role`, `ToolCall`, `ToolResult` — with `ContentProvenance` on every `ToolResult`
- [ ] `Tool` (abstract base), `ToolSpec` (lean: name, description, input_schema, risk_class, read_only, idempotent, approval_policy stub)
- [ ] `ToolRegistry` — lookup only
- [ ] `ToolExecutor` — full lifecycle (resolve → validate → permission check → approval stub → execute → provenance → audit)
- [ ] `PermissionChecker` protocol + `AllowlistPermissionChecker`
- [ ] `ModelClient` protocol + `BedrockModelClient`, lean capability descriptor (context tokens, parallel tool calls, cost/token)
- [ ] `SessionStore` protocol + Postgres-backed implementation, tenant/project_id on every record
- [ ] `AgentLoop` — unchanged core loop
- [ ] `AgentSpec`, `RunConfig`, `Runner`, `RunResult` — lean field set only
- [ ] Error taxonomy (`ModelError`, `ToolError`, `WorkflowError` families)
- [ ] `RunEvent` basic envelope + emission at `RunStarted` / `ModelCalled` / `ToolCalled` / `RunCompleted` / `RunFailed`
- [ ] Golden eval: one representative multi-step tool-using task
- [ ] Exit signal: the task completes reliably, fully logged to Postgres, tenant-tagged, provenance recorded on every tool result, reconstructable from `RunEvent`s alone — no spawning, no MCP, no sandbox, no durable approvals yet

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
)

runner = Runner(model_clients={"bedrock": BedrockModelClient(...)}, session_store=PostgresSessionStore(...))

result: RunResult = await runner.run(spec, task="...", config=config)

result.status   # completed | failed | max_turns_exceeded
result.output   # final text
result.events   # RunEvent stream for this run
result.usage    # tokens/cost, minimal in Phase 0
```

Everything below `Runner.run()` — `AgentLoop`, `ToolExecutor`, `ModelClient` — is an internal collaborator. Application code never touches them directly, which is the entire point of introducing `AgentSpec`/`Runner` in v0.2.

---

## 15. Next Step

Resolve the three items in §12 (ADR-06, ADR-13, ADR-18-is-fine-to-defer), then begin Phase 0 against the checklist in §13. Nothing in that checklist requires re-deciding a fundamental runtime contract later — `ContentProvenance`, the `ToolExecutor` lifecycle shape, and the `AgentSpec`/`Runner` boundary are all designed to grow (more fields, richer events, deeper policy) rather than be rebuilt when Phase 2 through 8 land.
