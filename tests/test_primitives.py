"""M1 gate: primitives, provenance and the error taxonomy (FR-2, FR-13, FR-16)."""

import pytest

from agentsdk import (
    AgentSDKError,
    ContentProvenance,
    InstructionAuthority,
    MaxTurnsExceeded,
    Message,
    ModelError,
    ModelTimeout,
    Origin,
    Role,
    TaintFlag,
    ToolCall,
    ToolError,
    ToolPermissionDenied,
    ToolResult,
    ToolValidationError,
    TrustZone,
    WorkflowError,
)
from agentsdk.primitives import unstorable_reason
from agentsdk.outcomes import (
    ApprovalRequired,
    Completed,
    Failed,
    InputRequired,
    InterruptionKind,
    Pending,
    RunInterruption,
    ToolExecutionOutcome,
)


# --- FR-2: roles match the persisted schema ---------------------------------


def test_role_values_match_messages_table_enum():
    # LLD 2.2 defines the column enum as exactly user/assistant/tool.
    assert {r.value for r in Role} == {"user", "assistant", "tool"}


# --- FR-2: the provenance invariant -----------------------------------------


def test_tool_result_requires_provenance():
    with pytest.raises(TypeError):
        ToolResult(tool_call_id="c1", content="hi")  # type: ignore[call-arg]


def test_tool_result_rejects_non_provenance():
    with pytest.raises(TypeError, match="must be a ContentProvenance"):
        ToolResult(tool_call_id="c1", content="hi", provenance="trusted")  # type: ignore[arg-type]


def test_internal_tool_default_matches_lld():
    p = ContentProvenance.internal_tool()
    assert p.origin is Origin.INTERNAL_TOOL
    assert p.instruction_authority is InstructionAuthority.DATA_ONLY
    assert p.trust_zone is TrustZone.TRUSTED_SOURCE
    assert p.taint_flags == frozenset()
    assert p.is_tainted is False


def test_every_tool_result_carries_exactly_one_provenance():
    result = ToolResult(
        tool_call_id="c1", content="echo", provenance=ContentProvenance.internal_tool()
    )
    assert isinstance(result.provenance, ContentProvenance)


# --- ADR-26: taint propagates through the model ------------------------------


def test_model_output_does_not_launder_taint():
    """The rule that makes provenance worth having: a model paraphrasing an
    untrusted source produces content that is still untrusted."""
    untrusted = ContentProvenance(
        origin=Origin.EXTERNAL_TOOL,
        instruction_authority=InstructionAuthority.DATA_ONLY,
        trust_zone=TrustZone.UNTRUSTED,
        taint_flags={TaintFlag.EXTERNAL_CONTENT, TaintFlag.PROMPT_INJECTION_RISK},
    )
    derived = ContentProvenance.from_model(untrusted)

    assert derived.origin is Origin.MODEL
    assert derived.trust_zone is TrustZone.UNTRUSTED, "taint was laundered by the model"
    assert TaintFlag.EXTERNAL_CONTENT in derived.taint_flags
    assert TaintFlag.PROMPT_INJECTION_RISK in derived.taint_flags
    assert derived.is_tainted


def test_model_output_takes_least_trusted_input_and_union_of_taint():
    clean = ContentProvenance.internal_tool()
    tainted = ContentProvenance.internal_tool().with_taint(TaintFlag.USER_CONTROLLED)
    derived = ContentProvenance.from_model(clean, tainted)

    assert derived.taint_flags == frozenset({TaintFlag.USER_CONTROLLED})
    # Model output never outranks the developer's own instructions.
    assert derived.instruction_authority is InstructionAuthority.ADVISORY


def test_model_output_from_clean_inputs_stays_clean():
    derived = ContentProvenance.from_model(ContentProvenance.internal_tool())
    assert derived.is_tainted is False
    assert derived.trust_zone is TrustZone.TRUSTED_SOURCE


def test_provenance_is_immutable():
    p = ContentProvenance.internal_tool()
    with pytest.raises(Exception):
        p.trust_zone = TrustZone.UNTRUSTED  # type: ignore[misc]


# --- FR-2: message shape -----------------------------------------------------


def test_message_normalises_sequences_to_tuples():
    msg = Message(
        role=Role.ASSISTANT,
        tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
    )
    assert isinstance(msg.tool_calls, tuple)
    assert isinstance(msg.tool_results, tuple)
    assert msg.tool_calls[0].name == "echo"


# --- FR-13: the taxonomy -----------------------------------------------------


@pytest.mark.parametrize(
    "error,family",
    [
        (ModelTimeout, ModelError),
        (ToolValidationError, ToolError),
        (ToolPermissionDenied, ToolError),
        (MaxTurnsExceeded, WorkflowError),
    ],
)
def test_errors_sit_under_the_right_family(error, family):
    assert issubclass(error, family)
    assert issubclass(error, AgentSDKError)


def test_families_are_disjoint():
    assert not issubclass(ToolError, ModelError)
    assert not issubclass(WorkflowError, ToolError)


# --- FR-16: the seams exist, mostly unreachable ------------------------------


def test_only_completed_and_failed_are_reachable_in_phase0():
    reachable = {
        cls.__name__
        for cls in (Completed, Failed, InputRequired, ApprovalRequired, Pending)
        if cls.reachable_in_phase0
    }
    assert reachable == {"Completed", "Failed"}


def test_outcomes_share_one_base_so_the_contract_never_changes():
    provenance = ContentProvenance.internal_tool()
    result = ToolResult(tool_call_id="c1", content="ok", provenance=provenance)
    assert isinstance(Completed(result=result), ToolExecutionOutcome)
    assert isinstance(
        Failed(error=ToolValidationError("bad args"), result=result),
        ToolExecutionOutcome,
    )


def test_run_interruption_type_exists_without_persistence():
    interruption = RunInterruption(
        kind=InterruptionKind.TOOL_APPROVAL, run_id="run-1", tool_call_id="c1"
    )
    assert interruption.interruption_id
    assert interruption.created_at.tzinfo is not None
    # Approval is one kind among several, not its own mechanism (ADR-29).
    assert InterruptionKind.CREDENTIAL_REQUIRED in set(InterruptionKind)


# --- storability: a primitive no store can hold (M5 round 3) ----------------
#
# json.loads is RFC-8259-correct and turns `1e400` into inf and a U+0000 escape
# into a NUL. JSONB holds neither, so before this the same run completed in
# memory and failed against Postgres -- breaking postgres.py's own promise that
# the loop cannot tell which store it is talking to. The check lives on the
# primitives so every provider gets it, not just the adapter that was fixed.

NUL = chr(0)
UNSTORABLE = [
    float("inf"),
    float("-inf"),
    float("nan"),
    "text with a " + NUL + " in it",
]


@pytest.mark.parametrize("value", UNSTORABLE, ids=lambda v: repr(v)[:20])
def test_unstorable_values_are_named(value):
    assert unstorable_reason(value) is not None


@pytest.mark.parametrize(
    "value", [1, 1.5, "ordinary", None, True, [1, "a"], {"k": [1.5, "v"]}, ()],
    ids=lambda v: repr(v)[:20],
)
def test_storable_values_pass(value):
    assert unstorable_reason(value) is None


def test_unstorable_values_are_found_however_deeply_nested():
    assert unstorable_reason({"a": [{"b": ({"c": float("inf")},)}]}) is not None
    assert unstorable_reason({"a" + NUL: 1}) is not None, "a KEY can be unstorable too"


def test_unstorable_reason_is_total():
    """It runs on boundary paths, so it must not raise -- including on a
    self-referential structure, which a naive recursive walk never returns from."""
    cyclic = {}
    cyclic["self"] = cyclic

    class Hostile:
        def __eq__(self, other):
            raise RuntimeError("hostile __eq__")

        def __hash__(self):
            return 0

    # The depth guard must DIAGNOSE this, not merely survive it. Without the
    # guard the walk recurses until it hits the interpreter limit and the outer
    # catch absorbs the RecursionError -- still total, but the reason degrades
    # to "could not be checked", and a run that blew the stack is not the same
    # event as one that nested too deeply.
    assert unstorable_reason(cyclic) == "nested more deeply than the store can accept"
    assert unstorable_reason({"k": Hostile()}) is None
    assert unstorable_reason([Hostile()]) is None


@pytest.mark.parametrize("value", UNSTORABLE, ids=lambda v: repr(v)[:20])
def test_a_tool_call_with_unstorable_arguments_flags_itself(value):
    """The same channel undecodable JSON already uses: empty arguments plus the
    reason they are empty, which ToolExecutor rejects before anything runs."""
    call = ToolCall(id="c1", name="echo", arguments={"n": value})
    assert call.arguments_error is not None
    assert "cannot be stored" in call.arguments_error
    # Cleared, not just flagged: the assistant message carrying this call is
    # persisted whether or not the executor runs it.
    assert call.arguments == {}


def test_a_tool_call_keeps_an_existing_arguments_error():
    call = ToolCall(id="c1", name="echo", arguments={}, arguments_error="bad JSON")
    assert call.arguments_error == "bad JSON"


def test_ordinary_tool_call_arguments_are_untouched():
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi", "n": 1.5})
    assert call.arguments == {"text": "hi", "n": 1.5}
    assert call.arguments_error is None


def test_a_message_refuses_content_no_store_can_hold():
    """Content has no error channel to travel on. Refusing it is what keeps the
    outcome the same with and without persistence; dropping the byte would edit
    the record NFR-3 calls authoritative."""
    with pytest.raises(ValueError, match="cannot be stored"):
        Message(role=Role.ASSISTANT, content="a" + NUL + "b")


def test_ordinary_message_content_is_untouched():
    assert Message(role=Role.ASSISTANT, content="ordinary").content == "ordinary"
    assert Message(role=Role.ASSISTANT, content=None).content is None
