# Custom Agent SDK — Project Master Document

The single entry point for this project: what it is, why it exists, everything decided, everything still open, and how the design actually got here. Supporting detail lives in the files listed in §0; this document is the one that stands on its own.

---

## 0. Document Map

| File | What it contains |
|---|---|
| `agent-sdk-open-decisions.md` | The original 18-question tradeoff worksheet, filled out |
| `agent-sdk-architecture-decisions.md` | First-pass ADR record from those 18 answers |
| `agent-sdk-master-design.md` (v0.1) | First consolidated design — superseded, kept for history |
| `agent-sdk-master-design-v0.2.md` | ADR-03 reversal (versioned replanning) applied — superseded |
| `agent-sdk-master-design-v0.3.md` | Current design of record — interface/sequencing corrections, `PrincipalContext`, provenance model rewrite |
| `agent-sdk-high-level-design.md` | System-level HLD — layers, components, data flow, non-functionals. Covers **all phases**. |
| `agent-sdk-low-level-design.md` | Implementable LLD — schemas, exact interfaces, sequence flows. Covers **Phase 0 only**; each later phase gets its own when it starts |
| `custom_agent_framework.py` | An early illustrative Python scaffold from before the ADR process — a sketch, not the Phase 0 implementation. Phase 0 code should follow the LLD, not this file. |
| **This document** | Project charter: vision, decisions, phases, and the process history tying it together |

---

## 1. Vision & Motivation

A model-agnostic agent framework, built in-house rather than adopted from a vendor, for **personal product development** (ADR-04) — not an NS/PwC engagement deliverable, no formal review gating at this stage.

**Three goals:**
- **Model-agnostic** — wireable to any LLM provider (Bedrock now, a bring-your-own endpoint next).
- **LangGraph-pluggable, not LangGraph-dependent** — usable as a node inside a larger LangGraph workflow, but functions identically standalone (ADR-01).
- **Manus AI-inspired** — an orchestrator that plans a task and spawns multiple subagents, each with an isolated context, then synthesizes their results.

**Why build this instead of adopting Claude Agent SDK:** close in mechanics, not in maturity. The honest gaps — tool-implementation polish, model-harness co-adaptation (the model was trained alongside Claude Code's specific harness, not this one), keeping pace with model releases, ecosystem breadth — are real and addressed structurally through the continuous improvement loop (§9), not assumed away. Where this design is ahead rather than behind: a DAG-aware planner with immutable versioned replanning, a shared evidence ledger with structural deduplication and supersede history, and a confidence-gated critic — a multi-specialist orchestration layer Agent SDK isn't trying to provide.

**Positioning, updated:** orchestration sophistication alone stopped being a differentiator once Claude Code's Dynamic Workflows (deterministic script-based multi-agent orchestration, launched ~May 26–28, 2026) and the OpenAI Agents SDK's `RunState`/sandbox model both shipped. Anthropic's own `/deep-research` skill now runs essentially the same fan-out/verify/synthesize pattern as this project's Research Analyst use case. The durable differentiator is **provider neutrality plus evidence-centric governance** — structured, provenance-tagged claims with correction history, not raw text a synthesizer has to re-interpret.

---

## 2. Target Use Cases

Validation gates, not independent projects — each stresses the architecture differently, and all three reduce to the same skeleton (plan → spawn scoped specialists → isolate context → collect structured results → synthesize).

| Use case | Shape | Proves |
|---|---|---|
| **Deep Research Analyst** | Parallel fan-out, read-only | Subagent isolation, evidence dedup, synthesizer, MCP-sourced browser/search tools |
| **Software Tester** (codebase QA) | Mixed parallel + sandboxed execution | Sandboxed tool execution, critic/verifier pattern, permission gating on semi-risky actions |
| **Agentic RAG pipeline build** | Strict sequential DAG | Dependency-aware planning, heaviest permission surface, durable approvals before destructive steps |

---

## 3. Two-Stage Project Structure

This is the load-bearing clarification for how the whole project is sequenced, so it's worth stating unambiguously:

**Stage 1 — SDK Completion (Phases 0–8).** Build the harness itself. No use case gets built as a real product during this stage — each phase proves its new capability against a disposable synthetic task, never the actual Research Analyst/Software Tester/Agentic RAG product. When Phase 8 is done, the SDK is done, full stop.

**Stage 2 — Use-Case Delivery.** Only after Stage 1 completes. Uses the finished SDK's public API (`AgentSpec`/`RunConfig`/`Runner`) to build the three real products:
1. **Research Analyst** first — needs the least beyond what Phases 0–4 already proved.
2. **Software Tester** second — leans on Phase 5/6 capabilities (sandboxing, real permission gating).
3. **Agentic RAG** last — a bridge, not a pure SDK-consumer; needs Track B (below) mature as well, not just the SDK.

**Two parallel tracks feed this:**
- **Track A (priority 1, gates Stage 2 entirely):** the core harness — Stage 1 in full.
- **Track B (independent, parallel, can start any time):** the data substrate — the RAG ingestion/retrieval pipeline. Doesn't depend on harness maturity and isn't depended on by it. Its relationship to the existing production RAG design (chunk-level dedup, hybrid retrieval, RAGAS eval, cache freshness) is still an open question (ADR-18) — reuse wholesale, adapt, or design fresh.

**Naming discipline worth keeping:** "Phase 2" always means Stage 1's Orchestrator/DAG phase. Stage 2 is never called "Phase 2" in conversation or planning, specifically to avoid that collision.

---

## 4. Architecture Overview

Five layers, each depending only on the layers below it (full detail: HLD §3–4):

| Layer | Owns | Key components |
|---|---|---|
| Public API | The only layer application code calls | `AgentSpec`, `RunConfig`, `Runner`, `RunHandle` (Phase 2+), `RunResult` |
| Control Plane | One run's execution sequencing | `AgentLoop`, `ContextAssembler`, `ModelClient`/`ModelRegistry`, `ToolExecutor`, `PolicyEngine`, `RuntimeHook` |
| Orchestration Plane *(Phase 2+)* | Multi-agent decomposition and scheduling | `Orchestrator`, `PlanVersion`, DAG scheduler, `SchedulerLimits`, subagent pool |
| Data / State Plane | All durable state, one Postgres deployment | `SessionStore`, `RunStateStore`, `EventStore`, `EvidenceStore`/`EvidenceLedger`, `ArtifactStore`, `ResourceCache` |
| Execution, Security & Cross-cutting | Identity, credentials, sandboxing, cost, telemetry | `PrincipalContext`, `CredentialBroker`, `WorkspaceManager`, `BudgetGovernor`, error taxonomy, eval harness |

**Non-negotiable security principle (ADR-17, ADR-26):**
> Provenance informs policy; it does not itself enforce policy. Model-generated content does not automatically clear taint inherited from its inputs.

The actual enforcement boundary is structural: `PolicyEngine → ToolExecutor → Approval/Credential/Sandbox/Network` — never "the model respected the label."

**Non-functional posture:** model-agnostic (canonical `ModelRequest`/`ModelResponse` contract), multi-tenant by construction (ADR-11, enforced not just tagged), fully auditable (`RunEvent` + `ExecutionManifest` + `RunStateStore` together), extensible without vendor lock-in (ADR-01), cost-governed (ADR-06, accepted), deterministic where it matters (ADR-02/03 — DAG-only, immutable versioned plans).

---

## 5. Stage 1 Phased Build Plan

| Phase | Capability delivered | Key new components |
|---|---|---|
| **0** | Single-agent loop end to end | Primitives + `ContentProvenance`, `ModelRequest`/`ModelResponse`, `ToolExecutor`, `ToolExecutionOutcome`, `RunInterruption` (type only), lean `RuntimeHook`, `AgentSpec`/`RunConfig`/`Runner`, error taxonomy, `RunEvent`, `ModelRegistry`, lean `ExecutionManifest`, `PrincipalContext` (metadata only) |
| **0/1** | Model-agnosticism actually proven | Second `ModelClient` adapter (blocked on BYO wire format); native Anthropic Messages adapter adds explicit prompt-cache markers and returns thinking blocks between turns; the `ModelClient` contract reports every attempt that received, or may have received, a response, so a call billed inside `send()` is no longer uncounted (DECISION-0e3ed41b) |
| **M9** *(before 2)* | Honest results | Scope specified separately by the owner; not yet in `SPEC.md` |
| **M10** *(before 2)* | Safe built-in tools | Scope specified separately by the owner; not yet in `SPEC.md` |
| **2** | Orchestration, DAG planning, replanning | `Orchestrator`, `PlanVersion` + `ReplanRequest` policy, `PlanNode` acceptance criteria, `SchedulerLimits`, budget model (ADR-06, accepted: run ceiling + per-child reservation + reclaim, soft enforcement in USD and tokens), `ArtifactRef`/`ArtifactStore`, `RunHandle` + event streaming, `ContextPolicy`; parallel execution of read-only tool calls under ADR-30 per-run/provider/tool concurrency limits |
| **3** | Evidence substrate, conditional verification | `EvidenceStore`/`EvidenceLedger`/`EvidenceClaim`/`EvidenceVersion`/`EvidenceVerification`, claim-lock + backoff, `EvidenceSourceVersion`, tenant/auth-scoped caching, ADR-15 calibration fix |
| **4** | MCP, identity, basic interruptions | Full MCP 2026-07-28 conformance, `ToolCatalog`/`ToolResolver`/`QualifiedToolName`, basic non-durable `ApprovalManager`, `DelegationGrant`/`CredentialBroker`; tenant-scoped skills and instruction bundles (versioned, stored, hashed into the `ExecutionManifest`, loaded on demand through a tool, never read from the local filesystem); Tier 2 built-in write/edit tools behind the `ApprovalManager` |
| **5** | Sandboxed execution | `WorkspaceManager`/`WorkspaceSpec` (defined before the provider), microVM provider, sandbox-produced artifacts/snapshots; Tier 3 built-in shell and code-execution tools, only inside the sandbox |
| **6** | Durable, sequential, destructive execution | Full `RunState` replay/idempotency, durable/restart-surviving `RunInterruption` and approvals |
| **7** | Context compaction | `ContextCompactor` — only once a subagent actually needs it |
| **8** | Production hardening | Full OpenTelemetry mapping, full `ExecutionManifest`-based compatibility enforcement, full eval matrix, finalized LangGraph adapter |

Each phase's exit signal and detailed rationale: `agent-sdk-master-design-v0.3.md` §7.

**Deferred capabilities and non-goals added 2026-09-12** by the owner's roadmap review against the Claude Agent SDK and the OpenAI Agents SDK. The full register is `agent-sdk-master-design-v0.3.md` §9.

| Capability | Lands in |
|---|---|
| Parallel execution of read-only tool calls, under ADR-30 concurrency limits | Phase 2 |
| Tenant-scoped skills and instruction bundles: versioned, stored, hashed into the `ExecutionManifest`, loaded on demand through a tool, never read from the local filesystem | Phase 4 |
| Tier 2 built-in write/edit tools, behind the `ApprovalManager` | Phase 4 |
| Tier 3 built-in shell and code-execution tools, only inside the sandbox | Phase 5 |
| Explicit prompt-cache markers; thinking blocks returned between turns | Phase 0/1, native Anthropic Messages adapter |
| Counting calls billed inside `send()` (an attempt that timed out after the provider processed it, a 2xx whose body cannot be read): the `ModelClient` contract reports every attempt that received, or may have received, a response | Phase 0/1, native Anthropic Messages adapter (DECISION-0e3ed41b; closes ASSUMPTION-b7463ca2) |
| Voice and realtime agents | Non-goal: not planned |

Recorded in Genesis as DECISION-f04449c9 (M9 and M10 before Phase 2), DECISION-79f09566 (a response cut off at the output-token limit ends the run as failed, reason max_tokens), DECISION-e6228dd4 (Phase 2 parallel read-only tool calls), DECISION-37bcac5b (Phase 4 skills and Tier 2 tools), DECISION-6a8204d0 (Phase 5 Tier 3 tools), DECISION-67b65b89 (Anthropic Messages adapter), DECISION-09edb52b (voice and realtime non-goal) and DECISION-f39da722 (ADR-06 timing, superseded by DECISION-e1bf0327: ADR-06 accepted).

---

## 6. Complete Architecture Decision Record

| ADR | Decision | Status |
|---|---|---|
| 01 | Subagent spawning: framework-first, LangGraph as optional adapter | Accepted |
| 02 | Plan representation: DAG-only, no classifier | Accepted |
| 03 | Plan mutability: immutable versioned plans + policy-controlled replanning | Accepted *(reversed from fixed-upfront)* |
| 04 | Project identity: personal product development | Accepted |
| 05 | Session/audit store: Postgres | Accepted |
| 06 | Recursion budget: run ceiling + per-child reservation + reclaim of unused budget, in USD and tokens, enforced softly (overshoot bounded by one model call per concurrently running agent) | Accepted — Phase 2 *(2026-09-12; inherited/split and flat cap rejected)* |
| 07 | Ledger versioning: supersede pointers | Accepted |
| 08 | Claim-lock: retry-on-failure + required backoff | Accepted |
| 09 | Sandbox: microVM, behind a `WorkspaceManager` abstraction | Accepted |
| 10 | Cost governor: per-subagent nested in per-session; global deferred | Accepted |
| 11 | Multi-tenancy: tag every record from day one | Accepted |
| 12 | Model-harness gap: test after every major phase | Accepted |
| 13 | Second provider: bring-your-own endpoint | **Proposed — needs wire format** |
| 14 | Tool-profile assignment: ad hoc per spawn | Accepted |
| 15 | Confidence threshold: adaptive, calibrated against externally validated outcomes (critic outcome is a signal, not the training label) | Accepted |
| 16 | Dedup scope: session ledger + global cache tier | Accepted |
| 17 | Prompt injection defense: provenance informs policy, never enforces it; taint propagates through model outputs; hard boundary is structural | Accepted |
| 18 | RAG substrate relationship to existing production design | **Open — not blocking, revisit when Track B starts** |
| 19 | Durable run state & replay semantics | Deferred — Phase 6 |
| 20 | Tool execution lifecycle (`ToolExecutor`) | Accepted — Phase 0 |
| 21 | Unified event/streaming model | Accepted (basic) — Phase 0; full taxonomy through Phase 8 |
| 22 | Observability / OpenTelemetry mapping | Deferred — Phase 8 |
| 23 | Durable human approval model | Resequenced — basic Phase 4, durable Phase 6 |
| 24 | Workspace, sandbox, artifact lifecycle | Split — `ArtifactRef` Phase 2, `WorkspaceManager` Phase 5 |
| 25 | Version pinning & compatibility | Clarified — lean `ExecutionManifest` Phase 0, full enforcement Phase 8 |
| 26 | Context assembly & taint propagation | Accepted — Phase 0 |
| 27 | Principal identity & delegated authority | Accepted (lean metadata) — Phase 0; full brokering Phase 4 |
| 28 | Tool catalog identity & integrity | Deferred — Phase 4 |
| 29 | Interruption model (`RunInterruption`) | Accepted (type only) — Phase 0; basic Phase 4; durable Phase 6 |
| 30 | Scheduler concurrency & backpressure | Deferred — Phase 2 |
| 31 | Evidence cache isolation & source versioning | Deferred — Phase 3 |

---

## 7. Open Items Requiring Your Input

1. **ADR-06** — accepted 2026-09-12 (§6; detail in `agent-sdk-master-design-v0.3.md` §4.7). Left open for the Phase 2 specification: the reservation cap rule, the fallback when the planner proposes no budget, and the sizes of the orchestrator reserve and the unallocated reserve.
2. **ADR-13** — wire format (OpenAI-compatible / Anthropic-compatible / custom) for the bring-your-own model endpoint. This is the only one actually blocking Phase 0/1 code.
3. **ADR-18** — not blocking; revisit when Track B starts.
4. **ADR-27** — gut-check on whether `PrincipalContext` earns a place in Phase 0 given no current use case clearly needs delegated authority yet. Low stakes either way — it's presently a free, unread metadata field.

---

## 8. Phase 0 Scope Summary

Full detail: `agent-sdk-low-level-design.md`. In brief:

**Data model (Postgres):** `runs`, `messages`, `run_events`, `execution_manifests`, `model_registry` — every table carries `tenant_id`/`project_id`, `NOT NULL`, indexed.

**Components:** primitives with `ContentProvenance`, `ModelRequest`/`ModelResponse`, `ToolSpec`/`ToolRegistry`/`ToolExecutor` (9-step lifecycle), `PermissionChecker`, lean `RuntimeHook`, `BedrockModelClient`, `ModelRegistry`, `ExecutionManifest`, Postgres-backed `SessionStore`, `AgentLoop`, `AgentSpec`/`RunConfig`/`Runner`/`RunResult`, error taxonomy, `RunEvent`.

**Golden eval:** an agent with one `echo` tool, asked to call it three times with different inputs and summarize the results — deliberately also triggering one permission denial and one validation failure, to exercise every Phase-0 error path in a single scenario.

**Exit signal:** the task completes reliably; every tool result carries provenance; the trace reconstructs from persisted state plus ordered events together (not events alone); every record is tenant-scoped; no subagent, MCP, sandbox, or durable approval exists yet.

---

## 9. Continuous Improvement Loop

A standing cycle, not a one-time hardening pass, instrumented from Phase 0:

**Capture** (every tool call, subagent result, critic outcome logged from day one) **→ Curate** (failures become regression fixtures; a golden eval suite grows per use case) **→ Gate** (any change — harness, tool, or model — must pass the golden suite, including a recurring side-by-side comparison against raw Claude Agent SDK, ADR-12) **→ Promote** (ship to production default; update the model registry) **→** new telemetry feeds back into Capture.

An internal skill/role registry grows from real use cases over time — the scoped-down, non-marketplace answer to Agent SDK's plugin ecosystem.

---

## 10. Process History — How This Design Got Here

| Stage | What happened |
|---|---|
| 1. Initial exploration | Compared Claude Agent SDK, LangGraph, and CrewAI; decided a custom framework was worth building, framework-first rather than LangGraph-native, with subagent isolation, MCP support, and context compaction as target capabilities |
| 2. Enterprise architecture brainstorm | Sketched a layered platform (entry point → orchestrator → subagent pool → shared services), Manus-AI-style multi-agent spawning as the core differentiator |
| 3. Use-case stress test | Ran three candidate use cases (research, QA, RAG pipeline building) against the skeleton — confirmed the same orchestrator/subagent/tool-profile pattern generalizes across all three |
| 4. Evidence & critic refinement | Designed the scratchpad (dedup + claim-lock), fact ledger with supersede pointers, and a confidence-gated critic invoked conditionally rather than on every result |
| 5. First phased build plan | Drafted Phases 0–8, sequenced so each phase proves the cheapest-to-catch risk first |
| 6. Continuous improvement loop | Designed Capture→Curate→Gate→Promote specifically to close the honest gaps against Claude Agent SDK over time rather than assume they'd disappear |
| 7. Program structure | Separated Track A (harness) from Track B (independent data substrate) and the Agentic RAG bridge |
| 8. 18 open decisions captured | A fill-in tradeoff worksheet (`agent-sdk-open-decisions.md`), answered, producing the first ADR record |
| 9. Master design v0.1 | First full consolidation of vision, architecture, phases, and ADRs into one document |
| 10. Enhancement review, round 1 | An uploaded literature review compared this design against Claude Agent SDK and OpenAI Agents SDK. Verified its 2026 vendor/protocol claims independently before acting on them. Triaged 25 recommendations into adopt-now (cheap, schema-level), sequence-later (real capability, wrong phase), and one proposed ADR reversal — flagged explicitly rather than silently adopted |
| 11. ADR-03 reversal confirmed | You confirmed switching from a fixed-upfront plan to immutable versioned plans with policy-controlled replanning, after seeing the exact mechanical tradeoff |
| 12. Master design v0.2 | Applied the confirmed reversal plus the "adopt now" bucket; deferred the rest into the phases that already needed them |
| 13. Enhancement review, round 2 | A second uploaded review caught genuine internal contradictions in v0.2 (an artifact contract with no defined type until Phase 5, despite Phase 2 already promising artifacts; an approval mechanism Phase 4's MCP work would need before Phase 5 built it). One of its "factual corrections" was itself checked against a primary source and found wrong — rejected rather than propagated |
| 14. Master design v0.3 | Current design of record: interface corrections (`ModelRequest`/`ModelResponse`), the provenance-informs-not-enforces principle, `ToolExecutionOutcome`/`RunInterruption` future-proof types, resequenced approval/artifact ownership, `PrincipalContext` introduced and flagged as the most speculative addition |
| 15. HLD + LLD | System-level design (all phases) and implementable Phase-0 detail (schemas, exact interfaces, sequence flows) — the last artifacts before code |
| 16. Two-stage clarification | Separated "completing the SDK" (Phases 0–8, synthetic proof tasks only) from "using the finished SDK to build the three real use cases" (a distinct later stage), to resolve an ambiguity in how the phase table's proof exercises had been read |
| 17. This document | Consolidates all of the above into one standing project reference |

---

## 11. Next Steps

1. Resolve §7's open items — ADR-13 (wire format) is the only one that actually blocks writing code.
2. Begin Phase 0 implementation against `agent-sdk-low-level-design.md`.
3. Write Phase 2's LLD only once Phase 0 is validated against its golden eval — not before.
4. Stage 2 (Use-Case Delivery) doesn't start until Phase 8 exits. Track B (data substrate) can start any time, independently, once ADR-18 is resolved.
