"""M1 gate: primitives, provenance and the error taxonomy (FR-2, FR-13, FR-16)."""

import json

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
from agentsdk.primitives import UNSTORABLE, unstorable_reason
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
UNSTORABLE_VALUES = [
    float("inf"),
    float("-inf"),
    float("nan"),
    "text with a " + NUL + " in it",
]


@pytest.mark.parametrize("value", UNSTORABLE_VALUES, ids=lambda v: repr(v)[:20])
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


@pytest.mark.parametrize(
    "value",
    [
        {"xs": ["fine", "bad" + NUL]},
        {"xs": ("fine", "bad" + NUL)},
        {"xs": [["fine"], ["bad" + NUL]]},
        {"xs": [{"k": "bad" + NUL}]},
    ],
    ids=["list", "tuple", "nested-list", "dict-in-list"],
)
def test_a_nul_inside_a_sequence_is_found(value):
    """A NUL is the case that separates the two layers: json.dumps escapes it
    happily, so only the named walk can catch it -- and only if the walk
    actually descends into sequences. Nothing pinned that; the earlier nesting
    test used an infinity, which the backstop catches on its own.
    """
    assert unstorable_reason(value) == "text contains a NUL character"


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

    # A cycle is caught by the serialiser's own circular-reference detection,
    # not by a depth cap. The cap used to answer this, and answered it for
    # honest deep structures too -- see the false-positive tests below.
    assert unstorable_reason(cyclic) == "cannot be serialised for storage: ValueError"
    # An object with no JSON representation is genuinely unstorable -- psycopg
    # raises TypeError on it too -- so being flagged is correct, not a false
    # positive. What matters here is that deciding that never raises.
    assert unstorable_reason({"k": Hostile()}) is not None
    assert unstorable_reason([Hostile()]) is not None


@pytest.mark.parametrize("value", UNSTORABLE_VALUES, ids=lambda v: repr(v)[:20])
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


# --- round 4: what the named checks did not name ----------------------------
#
# Round 3 named NUL and non-finite floats. A lone UTF-16 surrogate walked past
# all of them: a truncated escape is a legal RFC-8259 decode, models emit
# truncated pairs, and both TEXT and JSONB refuse the result. The named checks
# are now backed by attempting the serialisation the store performs, so what is
# pinned here is the CLASS -- values that cannot be serialised -- not a list.

# Built from the wire form a provider would actually send, not typed as a
# literal: the escape is what makes it a legal RFC-8259 decode.
LONE_SURROGATE = json.loads('"a' + chr(92) + 'ud800b"')


def test_a_lone_surrogate_is_unstorable():
    """Verified against Postgres: TEXT raises UnicodeEncodeError and JSONB
    raises InvalidTextRepresentation for exactly this value."""
    assert unstorable_reason(LONE_SURROGATE) is not None
    assert unstorable_reason({"text": LONE_SURROGATE}) is not None


def test_a_model_delivered_surrogate_flags_the_tool_call():
    call = ToolCall(id="c1", name="echo", arguments={"text": LONE_SURROGATE})
    assert call.arguments_error is not None
    assert call.arguments == {}


def test_surrogate_content_is_refused():
    with pytest.raises(ValueError, match="cannot be stored"):
        Message(role=Role.ASSISTANT, content=LONE_SURROGATE)


def test_an_integer_beyond_the_serialisers_limit_is_unstorable():
    """Not model-reachable -- json.loads refuses it before the SDK sees it, and
    _decode_arguments routes that to arguments_error -- but developer code can
    build one, and JSONB refuses it."""
    assert unstorable_reason(10 ** 5000) is not None
    assert unstorable_reason({"n": 10 ** 5000}) is not None


@pytest.mark.parametrize(
    "value",
    ["rocket " + chr(0x1F680), chr(0x2A6B2), int(1e308), 1.7976931348623157e308, "ok"],
    ids=["astral-emoji", "4-byte-CJK", "big-but-storable-int", "max-float", "plain"],
)
def test_values_postgres_accepts_are_not_rejected(value):
    """The other direction. A helper that over-rejects fails runs that should
    work, which is a defect too -- each of these was confirmed to store fine."""
    assert unstorable_reason(value) is None


def _nest(depth):
    value = {"leaf": 1}
    for _ in range(depth):
        value = {"n": value}
    return value


@pytest.mark.parametrize("depth", [10, 61, 100, 400], ids=lambda d: f"depth-{d}")
def test_deep_nesting_postgres_accepts_is_not_rejected(depth):
    """Found by probing rather than by review: the walk used to declare
    anything past depth 60 unstorable, while Postgres stores 900-deep JSON
    without complaint. A model returning deeply nested arguments had its tool
    call refused over a limit that does not exist -- over-rejection fails runs
    that should work, which is a defect in the same class as under-rejection.
    """
    assert unstorable_reason(_nest(depth)) is None


def test_nesting_near_the_recursion_limit_is_refused_deliberately():
    """A round-5 reviewer showed this helper and psycopg disagree in a window
    near the recursion limit (~969-975), because the limit is really "how much
    stack is left" and the two run at different depths -- so the window moves.

    Refusing early is the cheaper mistake. Disagreeing in the accepting
    direction means the write fails and the audit trail loses the message
    (NFR-3); disagreeing in the refusing direction costs an explicit tool error
    on nesting no model plausibly emits. This test pins that the margin exists
    and is a deliberate false positive, not a rediscovered depth cap: the band
    between it and the real limit IS refused, and that is the intended trade.
    """
    from agentsdk.primitives import _MAX_NESTING

    assert unstorable_reason(_nest(_MAX_NESTING - 10)) is None
    assert unstorable_reason(_nest(_MAX_NESTING + 10)) is not None
    assert unstorable_reason(_nest(2000)) is not None


def test_a_nul_is_found_however_deeply_it_is_buried():
    """Deferring on depth would have made the walk stop looking. It does not:
    the serialiser happily escapes a NUL, so only the named check finds this,
    and it has to still be looking at depth 300."""
    buried = _nest(300)
    cursor = buried
    for _ in range(300):
        cursor = cursor["n"]
    cursor["leaf"] = "deep" + chr(0) + "value"
    assert unstorable_reason(buried) == "text contains a NUL character"


# --- round 5: every field, not the fields someone thought of -----------------


def test_an_unstorable_tool_name_does_not_take_the_run_down():
    """A NUL in function.name reached the same JSONB column arguments did.
    Replaced rather than refused, so it becomes an ordinary "no such tool"
    error -- which is what happens without persistence -- instead of a run that
    dies at the write with no record of what the model said (NFR-3)."""
    call = ToolCall(id="c1", name="ec" + NUL + "ho", arguments={})
    assert call.name.startswith(UNSTORABLE)
    assert call.arguments_error is not None and "name" in call.arguments_error


def test_an_unstorable_tool_call_id_is_replaced():
    call = ToolCall(id="c" + NUL + "1", name="echo", arguments={})
    assert call.id.startswith(UNSTORABLE)
    assert call.arguments_error is not None and "id" in call.arguments_error


def test_an_unstorable_tool_result_id_is_replaced_without_losing_the_result():
    result = ToolResult(
        tool_call_id="c" + NUL + "1",
        content="the answer",
        provenance=ContentProvenance.internal_tool(),
    )
    assert result.tool_call_id.startswith(UNSTORABLE)
    assert result.content == "the answer", "a bad id must not destroy a good result"
    assert result.is_error is False


def test_an_unstorable_provenance_source_is_replaced():
    provenance = ContentProvenance.internal_tool(source_uri_or_hash="ha" + NUL + "sh")
    assert provenance.source_uri_or_hash.startswith(UNSTORABLE)


def test_every_string_field_is_checked_not_a_list_of_names():
    """The guarantee is structural: the walk covers dataclasses.fields(), so a
    string field added tomorrow is covered without anyone remembering. Assert
    that directly rather than trusting today's field list.
    """
    import dataclasses

    for cls, kwargs in (
        (ToolCall, dict(id="i", name="n")),
        (ToolResult, dict(tool_call_id="i", content="c",
                          provenance=ContentProvenance.internal_tool())),
    ):
        string_fields = [
            f.name for f in dataclasses.fields(cls)
            if f.type in ("str", "str | None")
        ]
        assert string_fields, f"{cls.__name__} has no string fields to check"
        for name in string_fields:
            instance = cls(**{**kwargs, name: "bad" + NUL})
            # Not "becomes the marker" -- ToolResult.content deliberately
            # becomes the reason instead, with is_error set. The property that
            # matters is that NO string field of a constructed primitive is
            # still unstorable, however that field is handled.
            assert unstorable_reason(getattr(instance, name)) is None, (
                f"{cls.__name__}.{name} is not covered by the storability walk"
            )


def test_two_unstorable_identifiers_do_not_collapse_into_one():
    """The marker used to be a single constant, so two tool calls with
    unstorable ids became the same string -- destroying the call-to-result
    correlation in the very record kept to explain what happened."""
    first = ToolCall(id="a" + NUL, name="echo", arguments={})
    second = ToolCall(id="b" + NUL, name="echo", arguments={})
    assert first.id != second.id
    assert first.id.startswith(UNSTORABLE) and second.id.startswith(UNSTORABLE)


def test_the_reason_channel_is_itself_storable():
    """arguments_error is written to JSONB like every other field. It used to
    be exempt from the walk -- a carve-out inside the mechanism whose purpose
    was to end carve-outs."""
    call = ToolCall(id="c1", name="echo", arguments={}, arguments_error="bad" + NUL + "json")
    assert unstorable_reason(call.arguments_error) is None


def test_an_unstorable_tool_result_content_is_marked_as_an_error():
    """Pins ToolResult's compensating check, which was a blind spot: with its
    skip=("content",) removed, the field walk marks content storable BEFORE
    this check runs, so is_error silently stays False. The shipped code is
    correct; nothing stopped a future cleanup of that skip -- round 6's exact
    defect shape -- from shipping green."""
    result = ToolResult(
        tool_call_id="c1",
        content="a" + NUL + "b",
        provenance=ContentProvenance.internal_tool(),
    )
    assert result.is_error is True
    assert "cannot be stored" in result.content
    assert unstorable_reason(result.content) is None


def test_configuration_refuses_rather_than_degrades():
    """Model output has a run to keep alive, so it is replaced and flagged.
    Configuration arrives before anything starts, so it is refused by name."""
    from agentsdk.identity import PrincipalContext

    with pytest.raises(ValueError, match="PrincipalContext.agent_principal cannot be stored"):
        PrincipalContext(agent_principal="ag" + NUL + "ent")


def test_container_fields_are_covered_not_just_strings():
    """The replacement walk only covers `str` fields. PrincipalContext.scopes
    is a tuple[str, ...] whose CONTENTS reach JSONB, and that is how this got
    through six rounds -- the round-6 prompt named it as the gap to attack and
    the round-7 reviewer found it there."""
    from agentsdk.identity import PrincipalContext

    with pytest.raises(ValueError, match="PrincipalContext.scopes cannot be stored"):
        PrincipalContext(agent_principal="a", scopes=("read", "wr" + NUL + "ite"))


def test_every_configuration_field_is_covered_whatever_its_shape():
    """Structural, like the model-output equivalent: derived from
    dataclasses.fields(), so a field added tomorrow is covered. Each field is
    given an unstorable value of a shape its own type can hold."""
    import dataclasses
    from agentsdk.identity import PrincipalContext

    base = dict(agent_principal="a")
    for f in dataclasses.fields(PrincipalContext):
        bad = ("x" + NUL,) if f.name == "scopes" else "x" + NUL
        with pytest.raises(ValueError, match=f"PrincipalContext.{f.name} cannot be stored"):
            PrincipalContext(**{**base, f.name: bad})
