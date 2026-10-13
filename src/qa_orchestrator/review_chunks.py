"""Byte-faithful Python AST chunks for host-owned Evidence Packets."""

import ast
import hashlib

from pydantic import BaseModel, ConfigDict


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


def chunk_source(source: bytes, *, path: str, max_bytes: int = 16000) -> tuple[ReviewChunk, ...]:
    if not 1 <= max_bytes <= 1_000_000 or len(source) > 4_000_000:
        raise ValueError("chunk budget/source size outside supported bounds")
    text = source.decode("utf-8")
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
    cursor = 1
    if tree is not None:
        for node in tree.body:
            start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))])
            end = node.end_lineno or node.lineno
            if start > cursor:
                units.extend((i, i, ()) for i in range(cursor, start))
            symbol = (
                (node.name,)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                else ()
            )
            if units and start <= units[-1][1]:
                previous = units.pop()
                units.append((previous[0], max(previous[1], end), previous[2] + symbol))
            else:
                units.append((start, end, symbol))
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(b"".join(lines[start - 1 : end]).decode())
            cursor = end + 1
    units.extend((i, i, ()) for i in range(cursor, len(lines) + 1))
    chunks, pending, size = [], [], 0

    def flush():
        if not pending:
            return
        start, end = pending[0][0], pending[-1][1]
        data = b"".join(lines[start - 1 : end])
        chunks.append(
            ReviewChunk(
                path=path,
                start_line=start,
                end_line=end,
                symbols=tuple(s for _, _, names in pending for s in names),
                text=data.decode(),
                sha256=hashlib.sha256(data).hexdigest(),
                strategy=strategy,
                oversized=len(data) > max_bytes,
                import_context="".join(imports),
            )
        )

    for unit in units:
        unit_size = sum(len(line) for line in lines[unit[0] - 1 : unit[1]])
        if pending and size + unit_size > max_bytes:
            flush()
            pending, size = [], 0
        pending.append(unit)
        size += unit_size
    flush()
    return tuple(chunks)
