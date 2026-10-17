"""Byte-faithful Python AST chunks for host-owned Evidence Packets."""

import ast
import hashlib

from pydantic import BaseModel, ConfigDict

MAX_SOURCE_UNITS = 100_000
MAX_REVIEW_CHUNKS = 10_000
MAX_IMPORT_CONTEXT_BYTES_PER_CHUNK = 16_000
MAX_TOTAL_IMPORT_CONTEXT_BYTES = 64_000


class ReviewChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    start_line: int
    end_line: int
    symbols: tuple[str, ...]
    text: str
    sha256: str
    strategy: str
    oversized: bool
    import_context: str
    import_context_truncated: bool = False


def chunk_source(source: bytes, *, path: str, max_bytes: int = 16000) -> tuple[ReviewChunk, ...]:
    if not 1 <= max_bytes <= 1_000_000 or len(source) > 4_000_000:
        raise ValueError("chunk budget/source size outside supported bounds")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("binary or non-UTF-8 source cannot be chunked") from exc
    if "\x00" in text:
        raise ValueError("binary or non-UTF-8 source cannot be chunked")
    parts = source.split(b"\n")
    lines = [p + b"\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    if not lines:
        return ()
    strategy = "lines_non_python"
    tree = None
    if path.endswith(".py") and b"\r" in source.replace(b"\r\n", b""):
        # Git evidence uses LF line numbers; bare CR has different Python AST coordinates.
        strategy = "lines_non_lf_python"
    elif path.endswith(".py"):
        try:
            tree = ast.parse(text.lstrip("\ufeff"))
        except (SyntaxError, ValueError):
            strategy = "lines_syntax_error"
        else:
            strategy = "python_ast"
    units, imports = [], []

    def add_unit(start: int, end: int, symbols: tuple[str, ...] = ()) -> None:
        if len(units) >= MAX_SOURCE_UNITS:
            raise ValueError("source produces too many review units")
        units.append((start, end, symbols))

    cursor = 1
    if tree is not None:
        for node in tree.body:
            start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))])
            end = node.end_lineno or node.lineno
            if start > cursor:
                for line in range(cursor, start):
                    add_unit(line, line)
            symbol = (
                (node.name,)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else ()
            )
            if units and start <= units[-1][1]:
                previous = units.pop()
                add_unit(previous[0], max(previous[1], end), previous[2] + symbol)
            else:
                add_unit(start, end, symbol)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(b"".join(lines[start - 1 : end]))
            cursor = end + 1
    for line in range(cursor, len(lines) + 1):
        add_unit(line, line)

    groups, pending, size = [], [], 0
    for unit in units:
        unit_size = sum(len(line) for line in lines[unit[0] - 1 : unit[1]])
        if pending and size + unit_size > max_bytes:
            if len(groups) >= MAX_REVIEW_CHUNKS:
                raise ValueError("source produces too many review chunks")
            groups.append(tuple(pending))
            pending, size = [], 0
        pending.append(unit)
        size += unit_size
    if pending:
        if len(groups) >= MAX_REVIEW_CHUNKS:
            raise ValueError("source produces too many review chunks")
        groups.append(tuple(pending))

    import_bytes = b"".join(imports)
    context_limit = min(
        MAX_IMPORT_CONTEXT_BYTES_PER_CHUNK,
        MAX_TOTAL_IMPORT_CONTEXT_BYTES // len(groups),
    )
    import_context = import_bytes[:context_limit].decode("utf-8", errors="ignore")
    context_truncated = len(import_context.encode("utf-8")) < len(import_bytes)
    chunks = []
    for group in groups:
        start, end = group[0][0], group[-1][1]
        data = b"".join(lines[start - 1 : end])
        chunks.append(
            ReviewChunk(
                path=path,
                start_line=start,
                end_line=end,
                symbols=tuple(s for _, _, names in group for s in names),
                text=data.decode(),
                sha256=hashlib.sha256(data).hexdigest(),
                strategy=strategy,
                oversized=len(data) > max_bytes,
                import_context=import_context,
                import_context_truncated=context_truncated,
            )
        )
    return tuple(chunks)
