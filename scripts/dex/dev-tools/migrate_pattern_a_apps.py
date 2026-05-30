"""Auto-migrate Lance emit Modal apps to use modal/_lib/pattern_a_lance_emit.py.

Detection: an app is considered a Pattern A Lance-emit if its source file
matches all of these criteria:
1. Filename matches `*_lance_emit_app.py`.
2. Imports `subprocess` (subprocess-based runner).
3. Defines `DISPLAY_NAME = "..."` constant.
4. Has `modal.Cron("...")` decorator schedule.
5. Has `EMIT_MEMORY_MB` or `MEMORY_MB` constant.
6. Has `_resolve_source_id` function (the ops.data_sources pattern).

Extraction: pulls app_name (from modal.App(...)), DISPLAY_NAME, cron schedule,
memory, timeout, and script_path (from subprocess.run([sys.executable, "<path>", "--apply"])).

Generates the compact replacement file. Preserves the original module
docstring (first triple-quoted block).

DRY-RUN mode: pass --dry-run to print proposed changes without writing.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MODAL_DIR = Path("apps/data-engine-x/modal").resolve()
SCRIPT_RE = re.compile(
    r"sys\.executable,\s*[\"'](/root/scripts/[^\"',]+)[\"']"
)
SCRIPT_CONST_RE = re.compile(
    r"SCRIPT_PATH\s*=\s*[\"'](/root/scripts/[^\"']+)[\"']"
)


def _safe_eval_const(node: ast.AST) -> int | str | None:
    """Best-effort constant resolution: literal_eval, BinOp arithmetic on
    int constants, and Name-references resolved via simple lookup."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        lhs = _safe_eval_const(node.left)
        rhs = _safe_eval_const(node.right)
        if isinstance(lhs, int) and isinstance(rhs, int):
            return lhs * rhs
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _extract_constant(tree: ast.Module, name: str) -> str | int | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return _safe_eval_const(node.value)
    return None


def _extract_modal_app_name(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "App"
        ):
            if node.value.args and isinstance(node.value.args[0], ast.Constant):
                return str(node.value.args[0].value)
    return None


def _extract_cron_schedule(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "Cron":
            if node.args and isinstance(node.args[0], ast.Constant):
                return str(node.args[0].value)
    return None


def _extract_script_path(source: str) -> str | None:
    m = SCRIPT_RE.search(source)
    if m:
        return m.group(1)
    m = SCRIPT_CONST_RE.search(source)
    if m:
        return m.group(1)
    # Derive from filename convention: <stem>_app.py → /root/scripts/run_<stem>.py
    return None


def _extract_first_docstring(source: str) -> str:
    """Return the first triple-quoted block (the module docstring) or empty
    if not present. Preserves the quotes."""
    m = re.match(r'^("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', source.strip())
    if m:
        return m.group(1) + "\n"
    return ""


def migrate_one(path: Path, dry_run: bool = False) -> tuple[bool, str]:
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return False, f"SYNTAX ERROR: {e}"

    app_name = _extract_modal_app_name(tree)
    display_name = _extract_constant(tree, "DISPLAY_NAME")
    memory_mb = (
        _extract_constant(tree, "EMIT_MEMORY_MB")
        or _extract_constant(tree, "MEMORY_MB")
    )
    timeout_s = (
        _extract_constant(tree, "EMIT_TIMEOUT_SECONDS")
        or _extract_constant(tree, "TIMEOUT_SECONDS")
    )
    cron = _extract_cron_schedule(tree)
    script_path = _extract_script_path(source)
    if not script_path:
        # Derive from filename: foo_lance_emit_app.py → /root/scripts/run_foo_lance_emit.py
        stem = path.stem  # e.g. fmcsa_carrier_essentials_lance_emit_app
        if stem.endswith("_app"):
            inferred = f"/root/scripts/run_{stem[:-4]}.py"
            inferred_path = Path("apps/data-engine-x") / inferred[1:]  # strip leading /
            if inferred_path.exists():
                script_path = inferred

    missing = []
    if not app_name: missing.append("app_name (modal.App(...))")
    if not display_name: missing.append("DISPLAY_NAME")
    if not memory_mb: missing.append("EMIT_MEMORY_MB")
    if not timeout_s: missing.append("EMIT_TIMEOUT_SECONDS")
    if not cron: missing.append("modal.Cron(...)")
    if not script_path: missing.append("script_path (sys.executable call)")
    if missing:
        return False, f"SKIP — missing: {', '.join(missing)}"

    if "_resolve_source_id" not in source:
        return False, "SKIP — no _resolve_source_id (not the canonical Pattern A shape)"

    # Decide if memory_mb is at default (4096) — if not, we set explicit.
    docstring = _extract_first_docstring(source)
    if not docstring:
        docstring = f'"""{Path(path).stem} — Pattern A Lance emit cron."""\n'

    # Memory/timeout kwargs only if non-default
    extra_kwargs = []
    if memory_mb and memory_mb != 4096:
        extra_kwargs.append(f"    memory_mb={memory_mb},")
    if timeout_s and timeout_s != 60 * 60:
        if timeout_s == 90 * 60:
            extra_kwargs.append("    timeout_seconds=90 * 60,")
        elif timeout_s == 2 * 60 * 60:
            extra_kwargs.append("    timeout_seconds=2 * 60 * 60,")
        else:
            extra_kwargs.append(f"    timeout_seconds={timeout_s},")
    extra_block = "\n" + "\n".join(extra_kwargs) if extra_kwargs else ""

    new_source = f"""{docstring}from __future__ import annotations

import json
import sys

import modal

# Local import — modal/_lib is mounted into the container at /root/_lib via
# build_image(); this sys.path tweak makes the same import work at deploy
# time when this file is imported by `modal deploy`.
sys.path.insert(0, "modal")  # noqa: E402  (must be before _lib import)

from _lib.pattern_a_lance_emit import (  # noqa: E402
    ORCHESTRATOR_SECRETS,
    PatternALanceEmitConfig,
    build_image,
    run_emit,
)

CONFIG = PatternALanceEmitConfig(
    app_name="{app_name}",
    script_path="{script_path}",
    display_name="{display_name}",
    cron_schedule="{cron}",{extra_block}
)

app = modal.App(CONFIG.app_name)
image = build_image(CONFIG)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=ORCHESTRATOR_SECRETS,
    timeout=CONFIG.timeout_seconds,
    memory=CONFIG.memory_mb,
    schedule=modal.Cron(CONFIG.cron_schedule),
)
def emit() -> dict:
    return run_emit(CONFIG)


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(emit.remote(), indent=2, default=str))
"""

    if dry_run:
        print(f"\n=== {path.name} ===")
        print(f"app_name={app_name}, display={display_name}, mem={memory_mb}, "
              f"timeout={timeout_s}, cron={cron!r}, script={script_path}")
        return True, "(would write)"

    path.write_text(new_source)
    return True, f"migrated → {len(new_source)} bytes (was {len(source)})"


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    candidates = sorted(MODAL_DIR.glob("*_lance_emit_app.py"))
    print(f"Found {len(candidates)} candidate apps.\n")

    migrated = 0
    skipped = 0
    for path in candidates:
        ok, msg = migrate_one(path, dry_run=dry_run)
        flag = "✓" if ok else "✗"
        print(f"{flag} {path.name}: {msg}")
        if ok:
            migrated += 1
        else:
            skipped += 1
    print(f"\n{migrated} migrated, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
