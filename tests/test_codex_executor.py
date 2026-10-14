import asyncio
import hashlib
import json
import sys
from dataclasses import replace

import pytest

from qa_orchestrator.contracts import ReviewAgent
from qa_orchestrator.host import HostProfileJob
from qa_orchestrator.orchestration import QaOrchestrator
from qa_orchestrator.review_profiles import build_review_route


@pytest.mark.parametrize("profile", list(ReviewAgent))
def test_output_schema_enforces_profile_finding_scope(profile):
    from qa_orchestrator.codex_executor import _output_schema

    schema = _output_schema(profile.value)
    limit = 0 if profile in {ReviewAgent.PR_TEST_ANALYZER, ReviewAgent.CODE_EXPLORER} else 100
    assert schema["properties"]["finding_candidates"]["maxItems"] == limit
    assert schema["properties"]["finding_candidates"]["type"] == "array"
    assert "finding_candidates" in schema["required"]
    assert schema["properties"]["profile"]["enum"] == [profile.value]


@pytest.mark.parametrize("profile", ["pr_test_analyzer", "code_explorer"])
async def test_executor_passes_role_constraint_to_cli(executor_input, profile):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    job = replace(job, profile=profile, route=build_review_route(profile))
    source = script.read_text().replace("'profile':'python_reviewer'", f"'profile':{profile!r}")
    source = source.replace("prompt=sys.stdin.read()", """schema=json.loads(pathlib.Path(a[a.index('--output-schema')+1]).read_text())
assert schema['properties']['finding_candidates']['maxItems']==0
prompt=sys.stdin.read()""")
    script.write_text(source)
    report = await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)
    assert report.profile == profile
    assert not report.finding_candidates


@pytest.mark.parametrize("profile", ["pr_test_analyzer", "code_explorer"])
async def test_executor_rejects_forbidden_findings_despite_schema(
    executor_input, review_finding_candidate, profile,
):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    job = replace(job, profile=profile, route=build_review_route(profile))
    source = script.read_text().replace("'profile':'python_reviewer'", f"'profile':{profile!r}")
    findings = json.dumps([review_finding_candidate.model_dump(mode="json")])
    source = source.replace("'finding_candidates':[]", f"'finding_candidates':json.loads({findings!r})")
    script.write_text(source)
    with pytest.raises(ValueError, match="CLI reviewer returned an invalid report") as error:
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)
    assert f"{profile} cannot report implementation findings" in str(error.value.__cause__)


def test_executor_schema_change_invalidates_previous_identity(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, _, _ = executor_input
    executor = CodexProfileExecutor(packet)
    old_identity = hashlib.sha256(json.dumps({
        "implementation": "codex-packet-executor-v4",
        "command": executor.command,
        "max_context_bytes": executor.max_context_bytes,
        "model": executor.model,
        "reasoning": executor.reasoning,
    }, sort_keys=True).encode()).hexdigest()
    assert executor.identity != old_identity


@pytest.fixture
def executor_input(tmp_path, bound_review_context):
    packet = tmp_path / "packet.md"
    packet.write_text("E1: Pinned source data.\n", encoding="utf-8")
    context = bound_review_context.model_copy(
        update={"evidence_packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest()}
    )
    session = QaOrchestrator(ttl_seconds=1800, max_sessions=10).start("ordinary_review", context)
    job = HostProfileJob(session, "python_reviewer", build_review_route("python_reviewer"), (), 1)
    script = tmp_path / "fake_codex.py"
    script.write_text(
        """import json,sys,pathlib
a=sys.argv
assert a[a.index('--sandbox')+1]=='read-only'
assert '--ignore-user-config' in a and '--ephemeral' in a
assert not any('bypass' in x for x in a)
prompt=sys.stdin.read()
assert 'Pinned source data.' in prompt
out=pathlib.Path(a[a.index('--output-last-message')+1])
out.write_text(json.dumps({'profile':'python_reviewer','scope':'Inspected packet','finding_candidates':[], 'coverage_gaps':[], 'unverified':[], 'evidence_map':[]}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':5}}))
""",
        encoding="utf-8",
    )
    return packet, job, script


async def test_executor_launches_readonly_and_validates_actual_process_output(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    executor = CodexProfileExecutor(packet, command=(sys.executable, str(script)))
    result = await executor(job)
    assert result.profile == "python_reviewer"
    assert result.execution_metrics.input_tokens == 12
    assert result.execution_metrics.output_tokens == 5


async def test_executor_reports_phases_and_usage_before_failed_completion(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.contracts import ProfileAttemptObservation

    packet, job, script = executor_input
    observed = ProfileAttemptObservation()
    job = replace(job, observation=observed)
    script.write_text(
        "import json,sys\nsys.stdin.read()\n"
        "print(json.dumps({'type':'turn.completed','usage':"
        "{'input_tokens':12,'cached_input_tokens':7,'output_tokens':5}}))\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(ValueError, match="reviewer failed"):
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)
    assert observed.input_tokens == 12
    assert observed.cached_input_tokens == 7
    assert observed.output_tokens == 5
    assert observed.preparation_seconds >= 0
    assert observed.model_seconds >= 0


async def test_executor_success_exposes_validation_phase(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.contracts import ProfileAttemptObservation

    packet, job, script = executor_input
    observed = ProfileAttemptObservation()
    await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(
        replace(job, observation=observed)
    )
    assert observed.validation_seconds >= 0


@pytest.mark.parametrize("phase", ["preparation", "validation"])
async def test_failed_phase_still_reports_elapsed_time(executor_input, phase):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.contracts import ProfileAttemptObservation

    packet, job, script = executor_input
    observed = ProfileAttemptObservation()
    if phase == "preparation":
        packet.write_text("changed")
    else:
        script.write_text(script.read_text().replace("print(json.dumps({'type':'turn.completed'",
                                   "out.write_text('invalid');print(json.dumps({'type':'turn.completed'"))
    with pytest.raises(ValueError):
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(
            replace(job, observation=observed))
    assert getattr(observed, phase + "_seconds") >= 0


async def test_missing_usage_in_any_turn_remains_unknown(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.contracts import ProfileAttemptObservation

    packet, job, script = executor_input
    observed = ProfileAttemptObservation()
    script.write_text(script.read_text().replace(
        "prompt=sys.stdin.read()", "prompt=sys.stdin.read()\nprint(json.dumps({'type':'turn.completed'}))"
    ))
    report = await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(
        replace(job, observation=observed))
    assert observed.input_tokens is None
    assert observed.output_tokens is None
    assert report.execution_metrics.input_tokens is None


async def test_executor_selects_context_and_binds_actual_subset_hash(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.context_pack import ContextFragment, ContextPacket
    from qa_orchestrator.contracts import EvidenceRecord

    packet, job, script = executor_input
    fragments = []
    for ref, text, required in (("E1", "Pinned source data.", True),
                                ("E2", "OMITTED_TEST_ONLY", False)):
        fragments.append(ContextFragment(
            record=EvidenceRecord(evidence_ref=ref, kind="other", locator="host:"+ref,
                                  sha256=hashlib.sha256(text.encode()).hexdigest()),
            text=text, required=required, profiles=() if required else ("pr_test_analyzer",),
        ))
    for ref, kind, text in (("E3", "source", "original source"), ("E4", "diff", "changed diff")):
        fragments.append(ContextFragment(
            record=EvidenceRecord(
                evidence_ref=ref, kind=kind, revision=job.session.review_context.head_sha,
                path=job.session.review_context.changed_files[0] if kind == "source" else None,
                sha256=hashlib.sha256(text.encode()).hexdigest(),
            ), text=text,
        ))
    packet.write_text(ContextPacket(fragments=fragments).model_dump_json())
    context = job.session.review_context.model_copy(update={
        "evidence_packet_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(),
        "evidence_refs": ("E1", "E2", "E3", "E4"),
        "evidence_registry": tuple(f.record for f in fragments),
    })
    job = replace(job, session=job.session.model_copy(update={"review_context": context}))
    script.write_text(script.read_text().replace(
        "prompt=sys.stdin.read()", "prompt=sys.stdin.read()\nassert 'OMITTED_TEST_ONLY' not in prompt"
    ))
    report = await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)
    assert report.context_selection.included_refs == ("E1", "E3", "E4")
    assert report.context_selection.omitted_refs == ("E2",)
    assert report.context_selection.source_packet_sha256 == context.evidence_packet_sha256


async def test_context_byte_budget_changes_executor_checkpoint_identity(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, _, _ = executor_input
    assert CodexProfileExecutor(packet, max_context_bytes=1000).identity != (
        CodexProfileExecutor(packet, max_context_bytes=2000).identity)


async def test_executor_refuses_changed_packet_before_process_launch(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    packet.write_text("tampered")
    with pytest.raises(ValueError, match="packet hash"):
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)


@pytest.mark.parametrize(
    "body",
    [
        "print('{bad json');sys.exit(1)",
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{}')",
        "print(json.dumps({'type':'turn.failed','error':{'message':'SECRET'}}))",
    ],
)
async def test_failure_never_becomes_a_clean_report(executor_input, body):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    script.write_text("import pathlib,sys,json\n" + body, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)
    assert "SECRET" not in str(caught.value)


async def test_tool_actions_are_rejected_even_with_valid_final_output(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    script.write_text(
        script.read_text().replace(
            "prompt=sys.stdin.read()",
            "print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'forbidden'}}),flush=True)\nprompt=sys.stdin.read()",
        )
    )
    with pytest.raises(ValueError, match="tool action"):
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)


async def test_cli_prompt_excludes_rules_outside_changed_paths(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.review_rules import ReviewRuleset

    packet, job, script = executor_input
    rules = ReviewRuleset.model_validate(
        {
            "rules": [
                {
                    "rule_id": "unscoped",
                    "kind": "guard",
                    "paths": ["unrelated/**"],
                    "condition": "Only unrelated files",
                    "text": "UNSCOPED_RULE_MUST_NOT_BE_SENT",
                    "rationale": "Confirmed scope",
                }
            ]
        }
    )
    context = job.session.review_context.model_copy(update={"review_rules": rules})
    job = replace(
        job,
        session=job.session.model_copy(update={"review_context": context}),
        review_rules=rules.for_paths(context.changed_files),
    )
    script.write_text(
        script.read_text().replace(
            "prompt=sys.stdin.read()",
            "prompt=sys.stdin.read()\nassert 'UNSCOPED_RULE_MUST_NOT_BE_SENT' not in prompt",
        )
    )
    report = await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(job)
    assert report.review_rules_sha256 == rules.digest


async def test_timeout_terminates_process_and_removes_workspace(executor_input, tmp_path):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    executor = CodexProfileExecutor(
        packet, command=(sys.executable, str(script)), timeout_seconds=0.1
    )
    with pytest.raises(TimeoutError):
        await executor(job)


async def test_host_default_policy_requires_explicit_cli_model(executor_input):
    from qa_orchestrator.codex_executor import CodexProfileExecutor
    from qa_orchestrator.orchestration import HostDefaultStagePolicy

    packet, job, script = executor_input
    session = job.session.model_copy(update={"model_policy": HostDefaultStagePolicy()})
    with pytest.raises(ValueError, match="explicit CLI model"):
        await CodexProfileExecutor(packet, command=(sys.executable, str(script)))(
            replace(job, session=session)
        )
    result = await CodexProfileExecutor(
        packet, command=(sys.executable, str(script)), model="test-model"
    )(replace(job, session=session))
    assert result.profile == "python_reviewer"


async def test_cleanup_stops_descendant_even_after_cli_parent_exits(executor_input, tmp_path):
    from qa_orchestrator.codex_executor import CodexProfileExecutor

    packet, job, script = executor_input
    marker = tmp_path / "descendant-lived"
    child = f"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).write_text('leaked')"
    script.write_text(
        f"import subprocess,sys\nsubprocess.Popen([sys.executable,'-c',{child!r}])\n",
        encoding="utf-8",
    )
    with pytest.raises((ValueError, TimeoutError)):
        await CodexProfileExecutor(
            packet, command=(sys.executable, str(script)), timeout_seconds=0.2
        )(job)
    await asyncio.sleep(1.2)
    assert not marker.exists()
