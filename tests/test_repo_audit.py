import json
import subprocess

from qa_orchestrator.repo_audit import audit_test_suite, build_repository_profile


def _git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.PIPE)


def _commit(repository, message):
    _git(repository, "add", "--all")
    _git(
        repository,
        "-c",
        "user.name=QA Orchestrator Tests",
        "-c",
        "user.email=qa-orchestrator@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD").decode().strip()


def _repository(tmp_path, files):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _commit(repository, "initial")
    return repository


def test_profile_detects_stack_tests_and_ci_without_copying_command_secrets(tmp_path):
    repository = _repository(
        tmp_path,
        {
            "pyproject.toml": '[project.optional-dependencies]\ntest = ["pytest"]\n',
            ".github/workflows/ci.yml": (
                "jobs:\n  test:\n    steps:\n"
                "      - name: run pytest for documentation\n"
                "      - run: echo pytest is mentioned here\n"
                "      - run: API_TOKEN=do-not-emit uv run pytest -q\n"
            ),
            "tests/test_api.py": "def test_api():\n    assert True\n",
            "src/api.py": "def get_value():\n    return 1\n",
        },
    )

    profile = build_repository_profile(repository, cache_dir=tmp_path / "cache")

    assert profile["revision"] == _git(repository, "rev-parse", "HEAD").decode().strip()
    assert profile["test_frameworks"] == ["pytest"]
    assert profile["test_inventory"]["paths"] == ["tests/test_api.py"]
    assert profile["ci_test_commands"] == [
        {"path": ".github/workflows/ci.yml", "line": 6, "command": "uv run pytest"}
    ]
    assert "do-not-emit" not in json.dumps(profile)


def test_profile_extracts_only_safe_package_test_script_metadata(tmp_path):
    repository = _repository(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "scripts": {"test:unit": "API_TOKEN=do-not-emit vitest run --coverage"},
                    "devDependencies": {"vitest": "*"},
                }
            ),
            "src/widget.ts": "export const widget = true;\n",
            "src/widget.test.ts": "test('widget', () => expect(true).toBe(true));\n",
        },
    )

    profile = build_repository_profile(repository, cache_dir=tmp_path / "cache")

    assert profile["test_frameworks"] == ["vitest"]
    assert profile["project_test_commands"] == [
        {"path": "package.json", "script": "test:unit", "command": "vitest"}
    ]
    assert "do-not-emit" not in json.dumps(profile)


def test_profile_detects_camel_case_test_files_without_matching_unrelated_names(tmp_path):
    repository = _repository(
        tmp_path,
        {
            "src/api/ApiTest.java": "class ApiTest {}\n",
            "src/api/TestAuth.cs": "class TestAuth {}\n",
            "src/api/Latest.java": "class Latest {}\n",
        },
    )

    profile = build_repository_profile(repository, use_cache=False)

    assert profile["test_inventory"]["paths"] == ["src/api/ApiTest.java", "src/api/TestAuth.cs"]


def test_profile_cache_reuses_stable_metadata_and_invalidates_test_inventory(tmp_path, monkeypatch):
    from qa_orchestrator import repo_audit

    repository = _repository(
        tmp_path,
        {
            "pyproject.toml": '[project.optional-dependencies]\ntest = ["pytest"]\n',
            "tests/test_api.py": "def test_api():\n    assert True\n",
        },
    )
    cache_dir = tmp_path / "cache"

    first = build_repository_profile(repository, cache_dir=cache_dir)
    (repository / "src").mkdir()
    (repository / "src/new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    second_revision = _commit(repository, "unrelated source change")

    read_blobs = repo_audit._read_blobs

    def fail_on_cache_hit(*_args, **_kwargs):
        raise AssertionError("cache hit should not read configuration blobs")

    monkeypatch.setattr(repo_audit, "_read_blobs", fail_on_cache_hit)
    second = build_repository_profile(repository, cache_dir=cache_dir)
    monkeypatch.setattr(repo_audit, "_read_blobs", read_blobs)

    assert first["cache"]["hit"] is False
    assert second["cache"]["hit"] is True
    assert second["revision"] == second_revision
    assert second["languages"] == [{"name": "python", "tracked_files": 2}]

    (repository / "tests/test_new.py").write_text(
        "def test_new():\n    assert True\n", encoding="utf-8"
    )
    _commit(repository, "add test file")
    third = build_repository_profile(repository, cache_dir=cache_dir)

    assert third["cache"]["hit"] is False
    assert third["test_inventory"]["paths"] == ["tests/test_api.py", "tests/test_new.py"]


def test_test_audit_reports_evidence_anchored_candidates_and_ignores_dirty_files(tmp_path):
    repository = _repository(
        tmp_path,
        {
            "tests/test_waits.py": ("import time\ndef test_waits():\n    time.sleep(1)\n"),
            "tests/test_skipped.py": (
                "import pytest\n"
                '@pytest.mark.skip("pending")\n'
                "def test_skipped():\n"
                "    assert True\n"
            ),
            "tests/test_duplicates.py": (
                "def test_first():\n    assert True\n\ndef test_second():\n    assert True\n"
            ),
            "tests/ui.test.ts": (
                'test("waits", () => {\n  setTimeout(() => {}, 10)\n})\n'
                'test.only("focus", () => {})\n'
            ),
        },
    )
    (repository / "tests/test_dirty.py").write_text(
        "import time\ndef test_dirty():\n    time.sleep(99)\n", encoding="utf-8"
    )

    audit = audit_test_suite(repository)
    candidates = audit["candidates"]
    categories = {candidate["category"] for candidate in candidates}
    locations = {(candidate["path"], candidate["line"]) for candidate in candidates}

    assert audit["revision"] == _git(repository, "rev-parse", "HEAD").decode().strip()
    assert audit["test_files_scanned"] == 4
    assert categories >= {
        "no_direct_assertion",
        "fixed_wait",
        "focused_test",
        "skipped_test",
        "duplicate_test_body",
    }
    assert ("tests/test_waits.py", 2) in locations
    assert ("tests/test_skipped.py", 2) in locations
    assert ("tests/test_dirty.py", 2) not in locations
    assert all(candidate["evidence_sha256"] for candidate in candidates)
    assert all("source" not in candidate for candidate in candidates)
    duplicate = next(item for item in candidates if item["category"] == "duplicate_test_body")
    assert duplicate["related"]["evidence_sha256"]
    assert audit["limitations"]


def test_python_audit_does_not_count_nested_tests_or_assertions_in_helpers(tmp_path):
    repository = _repository(
        tmp_path,
        {
            "tests/test_scope.py": (
                "def test_wrapper():\n"
                "    def helper():\n"
                "        assert True\n"
                "    helper()\n\n"
                "class Helper:\n"
                "    def test_not_collected(self):\n"
                "        pass\n\n"
                "class TestCase:\n"
                "    def test_asserted(self):\n"
                "        assert True\n"
            )
        },
    )

    audit = audit_test_suite(repository, use_cache=False)

    assert audit["tests_identified"] == 2
    assert {(candidate["category"], candidate["line"]) for candidate in audit["candidates"]} == {
        ("no_direct_assertion", 1)
    }


def test_repo_profile_is_available_through_the_cli(tmp_path, capsys):
    from qa_orchestrator.cli import main

    repository = _repository(
        tmp_path,
        {"tests/test_api.py": "def test_api():\n    assert True\n"},
    )

    result = main(["repo", "profile", "--repository", str(repository), "--no-cache"])

    assert result == 0
    profile = json.loads(capsys.readouterr().out)
    assert profile["repository"] == str(repository.resolve())
    assert profile["test_inventory"]["total"] == 1
    assert profile["cache"]["enabled"] is False
