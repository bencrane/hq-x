"""Generate a route×auth inventory from the live FastAPI app.

For each route, identify which auth dependency it requires by walking the
dependency tree. Output a Markdown table grouped by router prefix.
"""
import os, sys, inspect
from collections import defaultdict

os.environ.update({
    'DEX_DB_URL_POOLED': 'postgres://stub',
    'SUPER_ADMIN_JWT_SECRET': 'stub',
    'HQX_SUPABASE_JWKS_URL': 'https://stub/.well-known/jwks.json',
    'HQX_SUPABASE_ISSUER': 'https://stub',
    'ALLOWED_ORIGINS': 'https://app.opsinternal.com',
    'DEX_DEFAULT_ORG_ID': '533b70fd-a9e3-4617-bd4d-c7520d96295e',
})

from app.main import app
from fastapi.routing import APIRoute

# Names of known auth dependency callables.
AUTH_NAMES = {
    'require_flexible_auth': 'flexible (super-admin | hq-x Supabase)',
    'get_current_super_admin': 'super-admin',
    'verify_hqx_supabase_jwt': 'hq-x Supabase',
}

def classify_dep(dep) -> str | None:
    if dep is None:
        return None
    name = getattr(dep, '__name__', '')
    return AUTH_NAMES.get(name)


def walk_route_deps(route):
    """Walk the route's dependency tree and return the auth labels found."""
    found = []
    if not hasattr(route, 'dependant'):
        return found

    def recurse(dependant):
        # The endpoint function itself
        if dependant.call is not None:
            label = classify_dep(dependant.call)
            if label:
                found.append(label)
        # Sub-dependencies
        for sub in dependant.dependencies:
            recurse(sub)

    recurse(route.dependant)
    return found


def route_prefix(path: str) -> str:
    parts = path.strip('/').split('/')
    if len(parts) >= 3 and parts[0] == 'api':
        if parts[1] == 'v1':
            return f"/api/v1/{parts[2]}"
        return f"/api/{parts[1]}"
    if len(parts) >= 2 and parts[0] == 'api':
        return f"/api/{parts[1]}"
    return '/' + parts[0] if parts else '/'


groups = defaultdict(list)
unknown = []

for r in app.routes:
    if not isinstance(r, APIRoute):
        continue
    auth_labels = walk_route_deps(r)
    methods = sorted(r.methods - {'HEAD'})
    method = ','.join(methods) if methods else '-'
    label = ' + '.join(sorted(set(auth_labels))) if auth_labels else 'NO AUTH'
    prefix = route_prefix(r.path)
    groups[prefix].append((method, r.path, label))
    if not auth_labels:
        unknown.append((method, r.path, r.endpoint.__module__))

# Print the markdown report
lines = []
lines.append('# DEX Route × Auth Inventory')
lines.append('')
lines.append('Generated from the live FastAPI app object on the `feat/dex-auth-phase4-docs-and-inventory` branch.')
lines.append('')
lines.append(f'**Total routes:** {sum(len(v) for v in groups.values())}')
lines.append('')

for prefix in sorted(groups.keys()):
    rows = sorted(groups[prefix], key=lambda x: x[1])
    lines.append(f'## `{prefix}` ({len(rows)} routes)')
    lines.append('')
    lines.append('| Method | Path | Auth |')
    lines.append('|---|---|---|')
    for method, path, label in rows:
        lines.append(f'| `{method}` | `{path}` | {label} |')
    lines.append('')

lines.append('## Routes with NO AUTH dependency')
lines.append('')
if unknown:
    lines.append('These should be either intentionally public or flagged for review.')
    lines.append('')
    lines.append('| Method | Path | Module |')
    lines.append('|---|---|---|')
    for method, path, module in sorted(unknown, key=lambda x: x[1]):
        lines.append(f'| `{method}` | `{path}` | `{module}` |')
    lines.append('')
else:
    lines.append('_None — every route has at least one classified auth dependency._')
    lines.append('')

print('\n'.join(lines))
