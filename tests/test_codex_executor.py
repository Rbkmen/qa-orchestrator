import asyncio
import hashlib
import sys
from dataclasses import replace

import pytest

from qa_orchestrator.host import HostProfileJob
from qa_orchestrator.orchestration import QaOrchestrator
from qa_orchestrator.review_profiles import build_review_route


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
