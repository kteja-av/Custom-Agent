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
