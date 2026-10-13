import hashlib

from test_evidence import (
    pinned_evidence as pinned_evidence,  # noqa: PLC0414 -- pytest fixture export
)

from qa_orchestrator import evidence


def test_chunks_preserve_exact_bytes_coordinates_decorators_and_import_context():
    from qa_orchestrator.review_chunks import chunk_source

    source = b"import os\r\n\r\n@decorator\r\ndef f():\r\n    return 'hello'\r\n\r\ndef g():\r\n    return 2"
    chunks = chunk_source(source, path="src/api.py", max_bytes=40)
    assert b"".join(c.text.encode() for c in chunks) == source
    f = next(c for c in chunks if "f" in c.symbols)
    assert "@decorator" in f.text
    assert f.start_line == 3
    assert f.oversized is True
    assert f.sha256 == hashlib.sha256(f.text.encode()).hexdigest()
    g = next(c for c in chunks if "g" in c.symbols)
    assert "import os" in g.import_context
    assert g.end_line == 8


def test_syntax_errors_fall_back_visibly_without_losing_bytes():
    from qa_orchestrator.review_chunks import chunk_source

    source = b"def bad(:\nline 2\nline 3\n"
    chunks = chunk_source(source, path="bad.py", max_bytes=12)
    assert b"".join(c.text.encode() for c in chunks) == source
    assert all(c.strategy == "lines_syntax_error" for c in chunks)


def test_same_line_statements_are_not_duplicated():
    from qa_orchestrator.review_chunks import chunk_source

    source = b"a = 1; b = 2\n"
    chunks = chunk_source(source, path="api.py", max_bytes=1)
    assert b"".join(c.text.encode() for c in chunks) == source


def test_cr_only_source_uses_visible_fallback_and_git_line_coordinates():
    from qa_orchestrator.review_chunks import chunk_source

    source = b"a=1\rb=2\r"
    chunks = chunk_source(source, path="api.py", max_bytes=1)
    assert len(chunks) == 1
    assert chunks[0].text.encode() == source
    assert chunks[0].start_line == chunks[0].end_line == 1
    assert chunks[0].strategy == "lines_non_lf_python"


def test_chunk_cli_reads_pinned_git_blobs(tmp_path, pinned_evidence):
    import json

    repository, _, context, _ = pinned_evidence
    output = tmp_path / "chunks.json"
    assert (
        evidence.evidence_main(
            [
                "chunks",
                "--repository",
                str(repository),
                "--revision",
                context.head_sha,
                "--path",
                "src/api.py",
                "--max-bytes",
                "64",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["chunks"][0]["text"] == "first\r\nchanged\r\nlast"
