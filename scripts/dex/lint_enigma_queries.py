"""Lint check for Enigma GraphQL queries.

Two modes:

  python scripts/lint_enigma_queries.py
      Default mode. Refuses raw GraphQL query literals outside the
      approved registry (`app/providers/enigma_adapter/queries.py`).

  python scripts/lint_enigma_queries.py --check-catalog
      Catalog completeness. Every `*_QUERY` constant in queries.py must
      have a matching entry in `query_catalog.CATALOG`, and vice versa.

Exit codes:
  0 — clean
  1 — violation(s) found

Exemption: trailing `# enigma-lint: ignore` on the offending line is
honored. Use sparingly — intended for cassette fixtures that echo a
query field name as part of a fake response payload.

Scope:
- Walks the repo from the current working directory.
- Skips: .venv, __pycache__, node_modules, docs, supabase, trigger,
  .git, reports, tmp, site-packages.
- Considers only .py files.

Raw-query detection is deliberately conservative. A "raw query" is:
  (a) a string literal whose first non-whitespace token is `query` or
      `mutation` followed by a name or `{`; OR
  (b) a string literal that contains a catalog-known schema field name
      (e.g. `isFlaggedByWatchlistEntries`) AND looks like a GraphQL
      selection (contains `{` and `}`).
False positives are easier to exempt than false negatives are to catch,
so the rule errs on strict.

Directive: EXECUTOR_DIRECTIVE_ENIGMA_CREDIT_ACCOUNTING_COMMIT_3.md §5.3.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

APPROVED_FILES: set[Path] = {
    REPO_ROOT / "app" / "providers" / "enigma_adapter" / "queries.py",
    REPO_ROOT / "app" / "providers" / "enigma_adapter" / "query_catalog.py",
    # Transitional — app/providers/enigma.py is the frozen legacy provider
    # (see CLAUDE.md §Legacy Enigma operation-ID stubs trap). Phase 8 of
    # the Enigma work roadmap removes the file outright. Until then the
    # lint treats its ~15 legacy raw queries as grandfathered.
    REPO_ROOT / "app" / "providers" / "enigma.py",
}

SKIP_DIR_NAMES: set[str] = {
    ".venv",
    ".git",
    ".claude",  # worktrees and transient state
    ".gemini",
    "__pycache__",
    "node_modules",
    "supabase",
    "trigger",
    "site-packages",
    "reports",
    "tmp",
    "openFDA",
    "pdl",
}

# Only skip `docs/` and `scripts/` subtrees that live directly under the
# repo root. `scripts/` is skipped because it contains ad-hoc diagnostic
# scripts (phase probes, live harnesses) that predate the catalog lint
# and are not production code paths. If a script in `scripts/` ever
# graduates into a canonical call site, the query must move to
# `queries.py`.
SKIP_DIR_RELATIVE: set[Path] = {
    REPO_ROOT / "docs",
    REPO_ROOT / "scripts",
}

# Files that are themselves approved to contain catalog-known tokens or
# fake response bodies. Kept minimal — tests and fixtures inline *mocked*
# response JSON, not queries.
APPROVED_PATH_PREFIXES: list[Path] = [
    REPO_ROOT / "tests" / "providers" / "enigma_adapter" / "fixtures",
]

# Schema-field tokens that, combined with GraphQL-selection-shape,
# indicate a raw query. Extend cautiously — every entry increases false-
# positive surface.
CATALOG_FIELD_TOKENS: tuple[str, ...] = (
    "isFlaggedByWatchlistEntries",
    "appearsOnWatchlistEntries",
    "registeredEntities",
    "cardTransactions",
    "operatingLocations",
    "legalEntities",
)

_QUERY_PREFIX_RE = re.compile(r"^\s*(query|mutation)\s+[A-Za-z_]\w*\s*[\(\{]")
_LINT_IGNORE_RE = re.compile(r"#\s*enigma-lint\s*:\s*ignore")


def _should_skip_dir(path: Path) -> bool:
    if path.name in SKIP_DIR_NAMES:
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for skip in SKIP_DIR_RELATIVE:
        try:
            if resolved == skip or skip in resolved.parents:
                return True
        except ValueError:
            continue
    return False


def _is_approved_file(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved in APPROVED_FILES:
        return True
    for prefix in APPROVED_PATH_PREFIXES:
        try:
            resolved.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False


def _iter_python_files(root: Path) -> list[Path]:
    results: list[Path] = []

    def _walk(current: Path) -> None:
        if _should_skip_dir(current):
            return
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.is_dir():
                _walk(entry)
            elif entry.is_file() and entry.suffix == ".py":
                results.append(entry)

    _walk(root)
    return results


def _looks_like_query(literal: str) -> bool:
    # Rule 1: literal opens with `query Name(...)` / `query Name { ... }`
    # or `mutation Name...`. This is the canonical GraphQL operation
    # shape and catches every real Enigma query in the codebase.
    if _QUERY_PREFIX_RE.match(literal):
        return True
    # Rule 2: literal contains a catalog-known schema field token AND a
    # GraphQL selection body (both `{` and `}`). Catches anonymous
    # queries and fragments. Deliberately narrow to minimize false
    # positives against docstrings and prose.
    if "{" not in literal or "}" not in literal:
        return False
    return any(token in literal for token in CATALOG_FIELD_TOKENS)


def _extract_ignore_lines(source: str) -> set[int]:
    lines_with_ignore: set[int] = set()
    for idx, line in enumerate(source.splitlines(), start=1):
        if _LINT_IGNORE_RE.search(line):
            lines_with_ignore.add(idx)
    return lines_with_ignore


def scan_file(path: Path) -> list[str]:
    """Return error messages for this file. Empty list = clean."""
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    ignore_lines = _extract_ignore_lines(source)

    def _ignored(node: ast.AST) -> bool:
        # A multi-line string's ignore comment can sit on the opening
        # triple-quote line OR the closing line, so check the whole span.
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None) or start
        if start is None:
            return False
        for lineno in range(start, end + 1):
            if lineno in ignore_lines:
                return True
        return False

    errors: list[str] = []

    for node in ast.walk(tree):
        # Literal strings: bare assignments, docstrings, etc.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literal = node.value
            if _looks_like_query(literal):
                if _ignored(node):
                    continue
                snippet = literal.strip().splitlines()[0][:80]
                errors.append(
                    f"{path}:{node.lineno}: raw GraphQL query literal "
                    f"outside approved registry: {snippet!r}"
                )
        # gql("...") calls.
        if isinstance(node, ast.Call):
            func = node.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name == "gql" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _ignored(node):
                        continue
                    errors.append(
                        f"{path}:{node.lineno}: gql() call outside approved "
                        f"registry"
                    )

    return errors


def check_raw_queries(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _iter_python_files(root):
        if _is_approved_file(path):
            continue
        errors.extend(scan_file(path))
    return errors


# ---------------------------------------------------------------------------
# Catalog completeness check
# ---------------------------------------------------------------------------

_QUERY_CONST_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*_QUERY$")


def _extract_query_constants(queries_path: Path) -> list[str]:
    """Return names of module-level `*_QUERY` string assignments."""
    source = queries_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(queries_path))
    names: list[str] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            if node.target is not None:
                targets = [node.target]
            value = node.value
        if value is None:
            continue
        # Unwrap `"""...""".strip()` — AST sees that as a Call on a Constant.
        unwrapped = value
        if (
            isinstance(unwrapped, ast.Call)
            and isinstance(unwrapped.func, ast.Attribute)
            and unwrapped.func.attr in {"strip", "lstrip", "rstrip"}
        ):
            unwrapped = unwrapped.func.value
        if not (isinstance(unwrapped, ast.Constant) and isinstance(unwrapped.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and _QUERY_CONST_NAME_RE.match(target.id):
                names.append(target.id)
    return names


def _load_catalog_standalone() -> dict:
    """Load query_catalog.CATALOG without running the enigma_adapter package init.

    The package __init__.py pulls heavy runtime deps (psycopg_pool, httpx) that
    CI's lint stage shouldn't need. We build a minimal fake parent package so
    that `from . import queries` inside query_catalog.py resolves.
    """
    import importlib.util
    import sys
    import types

    pkg_name = "enigma_adapter_lint_shim"
    adapter_dir = REPO_ROOT / "app" / "providers" / "enigma_adapter"

    shim_pkg = types.ModuleType(pkg_name)
    shim_pkg.__path__ = [str(adapter_dir)]
    sys.modules[pkg_name] = shim_pkg

    try:
        queries_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.queries", adapter_dir / "queries.py"
        )
        assert queries_spec is not None and queries_spec.loader is not None
        queries_module = importlib.util.module_from_spec(queries_spec)
        sys.modules[f"{pkg_name}.queries"] = queries_module
        queries_spec.loader.exec_module(queries_module)

        catalog_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.query_catalog", adapter_dir / "query_catalog.py"
        )
        assert catalog_spec is not None and catalog_spec.loader is not None
        catalog_module = importlib.util.module_from_spec(catalog_spec)
        sys.modules[f"{pkg_name}.query_catalog"] = catalog_module
        catalog_spec.loader.exec_module(catalog_module)
        return catalog_module.CATALOG
    finally:
        for key in list(sys.modules.keys()):
            if key == pkg_name or key.startswith(f"{pkg_name}."):
                sys.modules.pop(key, None)


def check_catalog_completeness(
    queries_path: Path | None = None,
    catalog: dict | None = None,
) -> list[str]:
    """Return error messages. Empty list = pass."""
    if queries_path is None:
        queries_path = (
            REPO_ROOT / "app" / "providers" / "enigma_adapter" / "queries.py"
        )
    if catalog is None:
        catalog = _load_catalog_standalone()

    declared = set(_extract_query_constants(queries_path))
    registered = set(catalog.keys())

    try:
        display_path: Path | str = queries_path.relative_to(REPO_ROOT)
    except ValueError:
        display_path = queries_path

    errors: list[str] = []
    for name in sorted(declared - registered):
        errors.append(
            f"Query {name} is declared in {display_path} "
            f"but missing from query_catalog.CATALOG. Add an entry with "
            f"billable_entity_types, max_tier, max_cost_declared, variable_caps."
        )
    for name in sorted(registered - declared):
        errors.append(
            f"Catalog entry {name} references a query that does not exist in "
            f"{display_path}."
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-catalog",
        action="store_true",
        help="Run catalog completeness check instead of raw-query scan.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repo root to scan. Defaults to the repo detected from this file.",
    )
    args = parser.parse_args(argv)

    if args.check_catalog:
        errors = check_catalog_completeness()
        if errors:
            print("Catalog completeness check FAILED:", file=sys.stderr)
            for msg in errors:
                print(f"  - {msg}", file=sys.stderr)
            return 1
        print("Catalog completeness check passed.")
        return 0

    errors = check_raw_queries(args.root)
    if errors:
        print("Enigma raw-query lint FAILED:", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        print(
            "\nFix: move the query into "
            "app/providers/enigma_adapter/queries.py and register it in "
            "query_catalog.CATALOG. If this is a test cassette with a fake "
            "response, add a trailing `# enigma-lint: ignore` comment.",
            file=sys.stderr,
        )
        return 1
    print("Enigma raw-query lint passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
