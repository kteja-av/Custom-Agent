You are the independent L4 reviewer for a bounded task in a Genesis-governed
repository. You did not write this code and you must not trust its author's
claims about it.

**Use a fresh model.** Across M5 to M13, nearly every defect was found in a region
the previous reviewer had not examined. If you have reviewed this project
before, say so and ask for a different session. Re-derive the requirements from
`SPEC.md` rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The `PATH` prefix is required: `graphizer` shells out to `python3`, and without
the venv first on PATH that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need `DATABASE_URL` from `.env`, and the regression
  gate also needs `BASE_URL` and `MODEL_API_KEY`. **Never print credentials or a
  connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M14
  adds the optional `otel` extra, pinned in `requirements.txt` below `# --- test`
  (P2-D12).
- **Host:** Windows Developer Mode must be on (D7).
- **Live example 13** also needs a collector: Docker Desktop running and the
  `jaegertracing/jaeger:2.20.0` container from `scripts/README.md`.

**Only one test session at a time.** Every pytest session compares the store's
run and artifact ids at its start and end (AC-44, `tests/conftest.py`), and the
development database is shared: two sessions at once fail each other at
teardown. Before running tests or gates, check that no other `pytest`,
`genesis.mjs gate` or example process is running. Do not run anything else in
parallel with a session either, not even a short Python probe. M14's
timing tests measure lower bounds and are not built to share the machine. Run
your own probes with `-p no:cacheprovider`: Genesis hashes `.pytest_cache`, so a
cache write can make a gate read stale with no code change.

## Task under review

**M14-timings-and-telemetry**: every model and tool call records its timings, and
an optional OpenTelemetry exporter turns a run into a span tree without ever
affecting the run.

First review round. Requirements FR-57..FR-60, NFR-15, NFR-19 and AC-45..AC-47
are in `SPEC.md` under "Phase 2, increment 1" (M14). Decisions that bear on it:
- **DECISION-8793bdd7 (P2-D12):** the `otel` extra at 1.44.0, and semantic
  conventions v1.40.0. The SPEC decision table's heading was corrected at the
  readiness check to show it accepted.
- **DECISION-ea07ecbd**, taken at this milestone's pre-flight:
  - `RunStarted` records `parent_run_id`.
  - A model client may declare an optional `provider_name`, which `ModelCalled`
    records.

  FR-57, FR-58, FR-59, AC-45 and AC-46 were amended to match. The gap it closes:
  the exporter consumes only a `RunHandle` or `RunResult`, and neither those nor
  any event carried either value.
- **DECISION-7fcb7b1f:** live example 13 sends to a local Jaeger.
- **DECISION-40ae2d24:** `EXPECTED_EXAMPLES` gains 13 with this milestone.

The pre-flight is KNOWLEDGE-a18764bf. D13 applies (DECISION-2bad84bb): in-process
caller code is trusted.

**The M14 code is not committed.** HEAD is `22d71c6`. Review it with:

```bash
git diff 22d71c6 -- agentsdk tests scripts README.md SPEC.md requirements.txt pyproject.toml .env.example
git status --short   # new: agentsdk/telemetry.py, agentsdk/timings.py, scripts/13_telemetry.py,
                     #      tests/test_telemetry.py
```

## What changed

- `agentsdk/timings.py` (new): `wall_clock()` returns UTC ISO-8601 text;
  `now_ns()` and `elapsed_ms()` use `time.perf_counter_ns`.
- `agentsdk/loop.py`: `ModelCalled` gains six fields.
  - `queued_ms`: the wait for the provider slot, measured from before `async with
    self._model_slot()` to entering it.
  - `started_at` and `duration_ms`: taken around `send`, retries inside the
    client included.
  - `model`: what the request names in `model_settings["model"]`, which a
    `before_model` hook may have changed; else the run's recorded model; a named
    model that cannot be stored is ignored.
  - `provider` and `provider_name`: from new `AgentLoop` arguments.
- `agentsdk/executor.py`: `_CallState` gains `received_ns`, `queued_ms`,
  `started_at` and `started_ns`.
  - `run_prepared` records receipt before taking the slots, and the wait once
    they are granted.
  - Step 6 records the start.
  - Every `ToolCalled`, success and failure alike, carries `started_at`,
    `duration_ms` (step 6 to its emission) and `queued_ms`.
  - A call that never reached step 6 carries `None`, `0.0` and `0.0`.
- `agentsdk/api.py`:
  - `RunStarted` gains `parent_run_id`.
  - `_provider_name(client_key)` mirrors `_default_model_id`: read once, in
    `_open`, and `None` when the attribute is missing, raises, is not exact
    non-empty text, or cannot be stored.
  - `_drive` starts the run's clock. All three terminal emits add `started_at`
    and `duration_ms` through `_run_timing(control)`.
- `agentsdk/handle.py`: `RunControl.started_at` and `started_ns`.
- `agentsdk/telemetry.py` (new): `OpenTelemetryExporter(tracer_provider, *,
  max_queue_size=10000)`.
  - OpenTelemetry is imported at construction, and its absence raises
    `ImportError` naming the `otel` extra.
  - `export(run)` queues a `RunResult`'s events, or starts a task following a
    `RunHandle`'s `events()`; it never waits.
  - One worker thread takes events off a bounded `queue.Queue`. A full queue drops
    and counts in `dropped`; an exception on the worker or in the following task
    counts in `errors`.
  - A run's events are held until its terminal event, then built:
    - `invoke_agent`, timed from the terminal event and linked (an OpenTelemetry
      `Link`) to the parent run's `invoke_agent` when this exporter has already
      started it;
    - a `chat` span per `ModelCalled` and an `execute_tool` span per
      `ToolCalled`, timed from their own fields and parented to `invoke_agent`.
  - `flush(timeout)` waits until everything handed over is handled;
    `shutdown(timeout)` stops the worker.
- `scripts/13_telemetry.py` (new):
  - Offline, a scripted model's run goes to an in-memory exporter and is printed
    as a tree.
  - Live, a real model's run is also sent over OTLP/HTTP, and every export result
    is checked.
- `README.md`: a "Timings and telemetry" section with an SQL query over
  `run_events` (the dashboard route) and an exporter snippet, plus the example
  row. `scripts/README.md`: the row and the Jaeger setup.
- `.env.example`: `OTEL_EXPORTER_OTLP_ENDPOINT`. `requirements.txt`,
  `scripts/requirements.txt` and `pyproject.toml` (`otel` extra): the three
  1.44.0 pins.
- `SPEC.md`: the P2-D12 heading, and FR-57, FR-58, FR-59, AC-45 and AC-46 as
  amended by DECISION-ea07ecbd.
- Tests:
  - `tests/test_telemetry.py` (new, 78 tests). 70 were written before the
    implementation. 8 were added, and two assertions tightened, after the first
    mutation run (A10).
  - Existing tests edited, as NFR-15 requires them to be listed:
    - `tests/test_distribution.py`: `CONFIG_VARS` gains
      `OTEL_EXPORTER_OTLP_ENDPOINT`, and `EXPECTED_EXAMPLES` gains
      `13_telemetry.py` (FR-60).
    - `tests/test_run_handles.py` `shape()` and
      `tests/test_concurrency.py::test_max_concurrent_tools_1_reproduces_...` both
      compare two runs' event payloads, and now leave out `started_at`,
      `duration_ms` and `queued_ms`. Those fields differ between any two runs by
      nature. Every other field, FR-57's other additions included, is still
      compared (A3).

## What the author ran

1. **Pre-flight** (KNOWLEDGE-a18764bf):
   - **Baseline:** 1110 passed at `22d71c6`, in 136.07 s.
   - **Packages:**
     - installed the pins, and `pip check` is clean;
     - the resolver added packages and upgraded nothing.
   - **Semantic conventions:** the `opentelemetry-semantic-conventions` 0.65b0
     package carries schema versions up to 1.43.0, including 1.40.0. All twelve
     FR-59 attribute names and the three operation values are present in it.
   - **Two spec gaps**, decided by the owner as DECISION-ea07ecbd.
2. **Tests first.** Against the unchanged code, 69 failed and 2 passed.
   - **The passes:**
     - `import agentsdk` loads no OpenTelemetry, which was already true;
     - the pins test, since the pre-flight had added them.
   - **The failures:**
     - 22 × `No module named 'agentsdk.telemetry'`;
     - 27 × a missing FR-57 payload key, as `KeyError`: `provider_name` 11,
       `started_at` 7, `model` 4, `duration_ms` 2, `parent_run_id` 2,
       `queued_ms` 1;
     - 14 × the NFR-15 key-set check, `RunStarted` lacking `parent_run_id`;
     - 6 × missing module, script, README row or `.env.example` entry.
3. **After the implementation:**
   - **`tests/test_telemetry.py`, first run:** 70 passed and 1 failed, the premise
     of A2.
   - **The neighbouring suites** (`test_agent_loop`, `test_tool_executor`,
     `test_run_handles`, `test_concurrency`) with them: 15 failed, all two-run
     payload comparisons (A3).
   - **After A2 and A3:** 342 passed (telemetry 70, the example-list test 1,
     agent loop 77, tool executor 26, run handles 64, concurrency 104).
   - **Example 13 offline:** exit 0, every check passed. The span tree it printed
     was `invoke_agent calendar` with two `chat` and two `execute_tool` spans.
   - **The README SQL query** ran against the development database: 8 columns,
     0 rows at the time.
4. **Full suite**, first run on the finished implementation: 1 failed, 1179
   passed in 171.70 s (1180 = 1110 before M14 + 70).
   - **The failure:** M10's dependency guard
     (`tests/test_builtin_tools.py::test_no_module_imports_a_search_vendor_or_any_dependency_beyond_the_declared_ones`)
     reported `{'telemetry.py': ['opentelemetry']}` (A9).
   - **After the owner's decision on A9,** that test run alone: 1 passed in 0.56 s.
     The whole suite runs again as the regression gate (item 7).
5. **Mutation run**, 34 mutants against `tests/test_telemetry.py` (D1 also
   against M10's dependency guard), with `-x`.
   - **Checks:** every file restored and its SHA-256 verified, `tree restored:
     True`, and a scan before and after found every snippet exactly once and no
     mutant text left.
   - **First run:** 29 of 34 killed.
   ```
   L1  a provider-slot wait not measured                 -> provider limit records the wait
   L2  a model call's duration not measured              -> client taking 50 ms records at least 50 [memory]
   L3  a model call's duration includes its slot wait    -> SURVIVED (A10)
   L4  model ignores a hook's change                     -> model is what the client received [memory]
   L5  no recorded model when none is named              -> no model named records the FR-32 model [memory]
   L6  provider_name not recorded                        -> provider_name is the declared name [memory-a declared name]
   L7  an unstorable sent model recorded                 -> SURVIVED (A10)
   X1  a tool-slot wait not measured                     -> second call under one run slot records its wait [memory]
   X2  step 6 records no start                           -> timings on every ending [memory-completed]
   X3  a tool call's duration includes its slot wait     -> SURVIVED (A10)
   X4  a tool call records no wait                       -> second call under one run slot records its wait [memory]
   P1  RunStarted records no parent                      -> RunStarted records the parent run id [memory]
   P2  an unstorable provider name recorded              -> provider_name [memory-an unstorable name]
   P3  a run's duration not measured                     -> timings on every ending [memory-completed]
   P4  a run failed at the Runner boundary, no timing    -> timings on every ending [memory-failed by a raising hook]
   P5  a cancelled run records no timing                 -> timings on every ending [memory-cancelled during a tool call]
   P6  the loop is given no client key                   -> timings on every ending [memory-completed]
   P7  the provider name is read per model call          -> provider_name is read once per run
   P8  the run starts its clock late                     -> timings on every ending [memory-completed]
   T1  a handle's spans built on the event loop          -> failing telemetry path [memory-SimpleSpanProcessor over a blocking exporter]
   T2  a dropped event not counted                       -> full queue drops and counts
   T3  a worker exception not counted                    -> exception increments errors [a span processor that raises]
   T4  a child run never linked                          -> child run linked only by the exporter that started its parent
   T5  an unknown cost exported                          -> unpriced call has no cost attribute
   T6  a provider name always exported                   -> every FR-59 attribute
   T7  a chat span outside its run                       -> one invoke_agent span with children [a finished RunResult]
   T8  spans timed from the event envelope               -> one invoke_agent span with children [a finished RunResult]
   T9  the wrong semantic conventions version            -> every FR-59 attribute
   T10 input tokens read from completion                 -> every FR-59 attribute
   T11 no parent_run_id attribute                        -> every FR-59 attribute
   T12 OpenTelemetry imported with the module            -> exporter without the SDK names the otel extra
   T13 flush ignores followed handles                    -> SURVIVED (A10)
   T14 the failure reason exported                       -> SURVIVED (A10)
   D1  opentelemetry imported outside telemetry.py       -> M10 dependency guard (A9)
   ```
   - **After A10:**
     - `tests/test_telemetry.py` on the unchanged code: 78 passed in 19.19 s.
     - The five survivors rerun: 5 of 5 killed, tree restored, no mutant text
       left, **34 of 34 in all**.
     - Killed by: L3 the provider-limit test; L7 the unstorable-model test
       [memory-an unstorable model]; X3 the run-slot test [memory]; T13 the flush
       test; T14 the failure-reason test.
   - **Not mutated, and so not claimed:**
     - `MAX_OPEN_RUNS` and `MAX_REMEMBERED_RUNS` eviction;
     - the `TypeError` for an unsupported `export` argument;
     - `shutdown`'s return value;
     - `_window`'s fallback for an unparsable `started_at` or a non-finite duration.
6. **Live example 13**, against the gateway and a local
   `jaegertracing/jaeger:2.20.0` (digest `sha256:46a886260e04...`), with
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`.
   - **The run:** exit 0 and all six checks passed, including "the spans reached
     http://localhost:4318 over OTLP/HTTP", which requires every OTLP export to
     return `SUCCESS`.
   - **Its timings:** `completed` in 2950.9 ms. Two `openai.gpt-4o-mini` calls
     took 2002.1 and 941.2 ms, and two parallel tool calls about 2 ms each.
   - **Jaeger's HTTP API afterwards:** 1 trace for service `agentsdk-example-13`,
     with 5 spans (`invoke_agent calendar`, two `chat openai.gpt-4o-mini`,
     `execute_tool weekday`, `execute_tool day_of_year`). The task text appeared
     nowhere in the API response.
7. **Gates**, on the final tree, with `--timeout 600000` and no other test
   process running:
   ```
   $ genesis gate . M14-timings-and-telemetry --timeout 600000
   M14-timings-and-telemetry: passed executable gates
   unit        exit 0  2026-09-15T14:17:02Z -> 14:17:22Z  78 passed in 19.48s
   regression  exit 0  2026-09-15T14:17:22Z -> 14:20:07Z  1188 passed in 163.67s
   ```
   - **State:** both gates carry source hash `6ce35a78`, and `KICKOFF.md` reads
     `unit:pass, regression:pass, independent-review:pending`.
   - **Count:** 1188 tests = the 1110 before M14 plus the 78 in
     `tests/test_telemetry.py`.
   - **Unchanged:** the 1110 tests before M14 are unchanged apart from the edits
     listed under A3 and A9 and `test_distribution.py`'s two lists (FR-60).
   - **AC-44:** its session check passed with all of them.

## Issues the author found during M14

Labels `A` are the author's own and never share a reviewer's label.

| id | finding | disposition |
|---|---|---|
| A1 | A run's spans can only be built once its terminal event arrives, because only that event records when the run started (FR-57). So the full-queue test's first run, whose three events went to a queue of one, could lose its terminal event, and nothing would hold the worker | changed after the red run, before any implementation passed it: the test holds the worker with a hand-built finished `RunResult` of one terminal event, which an empty queue always accepts. The docstring says so |
| A2 | The FR-59 attribute test's premise failed. `CALL_USAGE` reports cache tokens and the test registry priced none, so `call_cost` returned None (`registry.py`: a count with no price makes the cost unknown) | the test registry now prices `cache_read` and `cache_write`. No implementation change |
| A3 | Two existing tests compare two runs' full event payloads; FR-57's timings differ by nature | the three timing fields are left out of those comparisons, and nothing else. NFR-15 names "the payload fields FR-57 adds" as allowed. The 15 failures cleared with no other change, so nothing else differed |
| A4 | FR-58 says a child run is "linked" to its parent's span | read as an OpenTelemetry span link, not a parent: the child's spans stay in their own trace |
| A5 | FR-58 and FR-60 leave the exporter's methods open | `export`, `flush(timeout)`, `shutdown(timeout)`, `dropped` and `errors`, fixed by the tests' docstring |
| A6 | AC-47 says "a raising exporter increments errors", but the SDK's own span processors catch and log an exception from a `SpanExporter` | an exporter raising under `SimpleSpanProcessor` is in the no-effect test. `errors` is tested with a span processor that raises in `on_end` (that exception does reach `span.end()` on the worker) and with a tracer provider that raises |
| A7 | FR-57 does not say where "the executor receiving the call" is | at `run_prepared`'s entry, immediately before the slots are requested: steps 1 to 5 are not queueing |
| A8 | Timings of a model call whose client raises `ModelError` | none: no `ModelCalled` is emitted (FR-57), and the terminal event still records the run's timing |
| A9 | M10's dependency guard allows only the standard library and the declared runtime dependencies in any module under `agentsdk/`, and `telemetry.py` imports `opentelemetry`, the optional `otel` extra (P2-D12) | owner decision, 2026-09-15: the guard allows `opentelemetry` in `telemetry.py` alone. Every other module keeps its list, so an import anywhere else still fails (mutant D1). Not hidden behind `importlib`, which would defeat the guard. `import agentsdk` loading no OpenTelemetry is tested separately (NFR-19) |
| A10 | Five survivors of the first mutation run had no test: L3 and X3 (a duration that includes its slot wait), L7 (an unstorable hook-set model recorded as the model), T13 (`flush` returning before a followed handle ends), T14 (a failure reason exported) | tests only, no implementation change. The provider-limit test asserts the second call's duration is below its wait. The run-slot test makes its second call instant and asserts the same. New: a hook naming an unstorable, non-text or empty model records the run's model on both stores (6 tests); `flush(0.2)` returns False while a followed run is held; a failed run whose reason carries a marker exports none of it |

## Attack these first

**Report every item below as "probed", with what you ran and saw, or "not
probed", with why.** An approval that is silent on an item will be read as not
probed.

1. **The event loop.** Anything telemetry does on the loop's thread:
   - `export()` with a full queue;
   - a caller's `SimpleSpanProcessor` over an exporter that blocks forever;
   - `export()` called with no running loop, or from another thread;
   - many handles followed at once.
2. **No effect on a run.** Status, result, events and stored rows with and without
   the exporter, on both stores, under each failing setup in AC-47. Add any you
   think is missing, for example a `TracerProvider` whose `get_tracer` blocks.
3. **Content.** Every attribute, span name, status description, event and link of
   every span, for a run whose task, instructions, tool arguments, tool results,
   answer and failure reason all carry markers and credentials.
4. **Timings.** Each field against its definition in FR-57 on both stores:
   - retries inside `send`;
   - a model call cancelled while waiting for a provider slot;
   - a tool call cancelled after step 6;
   - a hook-replaced result;
   - timeouts;
   - a run failed at the Runner boundary.

   Check that `duration_ms` never includes `queued_ms`.
5. **`model`, `provider_name` and `parent_run_id`:**
   - a hook setting a non-text or unstorable model;
   - a client whose `provider_name` changes between reads;
   - a `uuid.UUID` parent.
6. **The span tree:**
   - a child run linked by one exporter and not another;
   - a child that ends before its parent;
   - a run exported twice, and a run exported both as a handle and as a result;
   - `MAX_OPEN_RUNS` and `MAX_REMEMBERED_RUNS` eviction.
7. **NFR-15.** Existing behaviour apart from FR-57's fields, and whether the two
   test edits (A3) hide anything else.
8. **NFR-19.** `import agentsdk` in a fresh interpreter, and the exporter with
   `opentelemetry.sdk` unimportable.
9. **The examples and docs:**
   - example 13 offline from outside the repository, and live against Jaeger;
   - the README SQL query on a database with M14 events.

## Declared limitations: known, recorded, NOT findings

- **Spans wait for the run to end.** A run's spans appear only once its terminal
  event is handled. A run that never ends, or whose terminal event is dropped,
  exports none. A child run that ends before its parent is not linked, and carries
  `agentsdk.parent_run_id` only.
- **Bounded memory.** The exporter keeps at most 10,000 runs awaiting their
  terminal event and 10,000 parent span contexts, oldest out; an evicted open
  run's events count as `dropped`.
- **No run totals on `invoke_agent`.** It carries no run-level usage or cost;
  those are on the `chat` spans.
- **`errors` does not see everything.** An exception inside a caller's
  `SpanExporter` under the SDK's own processors is logged by the SDK and not
  counted (A6).
- **Untested claim.** That a timing never changes a run's outcome is not tested by
  fault injection.
- **Semantic conventions.** Attribute names were checked in the 0.65b0 package,
  not against the v1.40.0 registry itself.
- **Earlier limitations** stand: DECISION-29e21dd0, P2-D7, and the M13 caveats in
  KNOWLEDGE-8b58092d.
- **Stale gates.** Older `independent-review` gates compute `stale` because the
  repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs.
- **Gates are computed, never narrated.** Paste real output for anything asserted.
- **Approve if it is sound.** A defect must be reachable and must matter; record
  latent, out-of-scope or cosmetic findings as caveats rather than blocking.
- **Mutating files.** Restore every file you mutate and verify SHA-256, restoring
  in a `finally`, and kill the whole process tree on a timeout (`taskkill /T`). Do
  not mutate a migration file: the live database records its checksum.
- **Clean up.** Remove every run, artifact, container, temporary folder and git
  worktree you create, artifacts before the runs they name.
- **Report every probe**, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$`
out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M14-timings-and-telemetry --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M14-timings-and-telemetry --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found per attack item, and
your verdict.
