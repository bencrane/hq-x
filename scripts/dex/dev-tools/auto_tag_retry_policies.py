"""Auto-tag every @app.function in apps/data-engine-x/modal/*.py with a
`# retry-policy: <class>` comment.

Detection logic:
1. If decorator has `retries=modal.Retries(max_retries=N, ...)` kwarg → tag as
   `modal-retries-transient` (decorator-level retries: the right place).
2. If decorator has `schedule=modal.Cron(...)` + long timeout (>1800s) → tag
   as `no-retry-orchestrator` (long-running orchestrator; retries belong on
   worker functions it invokes).
3. Otherwise → tag as `no-retry` (fast crons, one-shot emits, alerter).

The comment is inserted on the line immediately preceding the decorator.
Idempotent: skips functions that already have a comment matching POLICY_RE.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MODAL_DIR = Path("apps/data-engine-x/modal").resolve()
POLICY_RE = re.compile(r"#\s*retry-policy:\s*[a-z][a-z0-9_\-]*")


def _is_app_function_decorator(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    return isinstance(func, ast.Attribute) and func.attr == "function"


def _has_kwarg(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


def _get_kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _detect_policy(decorator: ast.Call) -> str:
    # Rule 1: decorator-level retries
    if _has_kwarg(decorator, "retries"):
        return "modal-retries-transient"

    # Rule 2: long-running orchestrator (schedule + long timeout)
    has_schedule = _has_kwarg(decorator, "schedule")
    timeout_node = _get_kwarg(decorator, "timeout")
    timeout_s = None
    if timeout_node is not None:
        try:
            timeout_s = ast.literal_eval(timeout_node)
        except (ValueError, TypeError):
            timeout_s = None
    if has_schedule and timeout_s is not None and timeout_s >= 1800:
        return "no-retry-orchestrator"

    return "no-retry"


def _scan_and_tag(path: Path) -> tuple[int, int]:
    """Returns (tags_added, already_tagged)."""
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        print(f"SKIP (syntax error): {path}")
        return 0, 0

    source_lines = source.splitlines(keepends=True)
    # We insert tags top-down; insertions shift line numbers. Collect first,
    # apply in reverse line order so earlier insertions don't shift later ones.
    insertions: list[tuple[int, str]] = []  # (zero-based line index, content)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not _is_app_function_decorator(dec):
                continue
            # Check if the decorator block (line above through first body line)
            # already has a policy comment.
            start_line = max(0, dec.lineno - 2)
            end_line = min(
                len(source_lines),
                (node.body[0].lineno if node.body else dec.lineno + 1) + 1,
            )
            block = "".join(source_lines[start_line:end_line])
            if POLICY_RE.search(block):
                continue
            policy = _detect_policy(dec)
            # Insert at the line index where the decorator's `@` lives.
            # dec.lineno is 1-based; line index is dec.lineno-1. We want to
            # insert ABOVE the decorator, so insert AT dec.lineno-1.
            # Find the actual `@` line — the decorator's lineno is the start
            # of the Call expression, not the `@`. In practice for
            # `@app.function(...)` they're the same line.
            insert_at = dec.lineno - 1
            # Match indentation of the decorator line.
            indent = ""
            if insert_at < len(source_lines):
                line = source_lines[insert_at]
                indent = line[: len(line) - len(line.lstrip())]
            # The decorator line starts with `@`; we need the line where `@`
            # is, which equals dec.lineno - 1 (zero-indexed). The decorator's
            # `lineno` is the line of the `@<name>(...)` expression itself.
            comment_line = f"{indent}# retry-policy: {policy}\n"
            insertions.append((insert_at, comment_line))

    if not insertions:
        return 0, 0

    # Apply in reverse so earlier indices don't shift.
    for insert_at, comment_line in sorted(insertions, reverse=True):
        source_lines.insert(insert_at, comment_line)

    path.write_text("".join(source_lines))
    return len(insertions), 0


def main() -> int:
    if not MODAL_DIR.is_dir():
        print(f"FATAL: {MODAL_DIR} not found", file=sys.stderr)
        return 2

    total_tagged = 0
    files_changed = 0
    for path in sorted(MODAL_DIR.glob("*.py")):
        added, _ = _scan_and_tag(path)
        if added:
            files_changed += 1
            total_tagged += added
            print(f"  tagged {added} in {path.name}")

    print(f"\nDone: {total_tagged} tags added across {files_changed} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
