"""Postgres-advisory lock for concurrent Lance writers on object storage.

Lance's commit protocol on object storage (R2 / S3) requires that only one
writer mutates the dataset at a time. Lance OSS uses an "external commit
handler" hook for this. On a single writer (Modal cron), the contention
window is tiny — but we still wire a Postgres-advisory lock so:

  1. Concurrent emits from the local dev machine + the Modal cron don't
     race during the rare manual reprocess.
  2. The cron's exception path can't ever leave a half-finished commit
     visible while a follow-up run starts.

The lock key is a stable hash of the dataset slug (e.g.
``fmcsa_carrier_essentials_lance``). We use ``pg_advisory_xact_lock`` so
the lock is automatically released at transaction end (commit OR rollback);
this means the caller MUST hold the Postgres transaction open for the entire
Lance write window. A bare-context-manager API enforces this.

Usage:

    from scripts._lib.lance_commit_lock import lance_commit_lock

    with lance_commit_lock("fmcsa_carrier_essentials_lance"):
        ds = lance.write_dataset(tbl, uri, ...)
        # commit happens inside this block; lock held until exit

The advisory lock is non-blocking for the SAME connection that holds it
(re-entrant within a Postgres connection's xact), but blocks any OTHER
connection trying to acquire the same key.

Reference: per
``~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/infra_budget_pre_revenue.md``
(Lance OSS operational discipline shipped as code, not as a follow-up
checklist).
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from typing import Iterator

import psycopg

LOG = logging.getLogger(__name__)


def _hashtext_int(slug: str) -> int:
    """Stable signed-int32 hash of the slug, matching Postgres ``hashtext``.

    We don't actually use Postgres's ``hashtext`` here — we compute the hash
    client-side so the lock key is deterministic without round-trip latency
    and is portable across the small number of places that might want to
    inspect it. The output is an int32-compatible signed integer.
    """
    h = hashlib.blake2b(slug.encode("utf-8"), digest_size=4).digest()
    # Interpret 4 bytes as signed int32 (big-endian).
    val = int.from_bytes(h, "big", signed=True)
    return val


@contextlib.contextmanager
def lance_commit_lock(slug: str) -> Iterator[None]:
    """Acquire a Postgres advisory lock keyed on ``slug`` for the duration.

    Required env: ``DEX_DB_URL_DIRECT`` (or ``DATABASE_URL`` fallback).
    Uses ``pg_advisory_xact_lock(int)`` which is blocking (waits) — the
    caller is expected to be the only Lance writer for the dataset, so
    contention is exceptional. Lock is auto-released on transaction end.
    """
    db_url = (
        os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DATABASE_URL")
    )
    if not db_url:
        raise EnvironmentError(
            "DEX_DB_URL_DIRECT (or DATABASE_URL) is required for the "
            "Lance commit lock"
        )
    key = _hashtext_int(slug)
    LOG.info("lance commit lock: slug=%r key=%d acquiring ...", slug, key)
    conn = psycopg.connect(db_url, autocommit=False)
    try:
        # Supabase prod defaults to statement_timeout='2min'; the advisory-lock
        # wait can exceed that if a prior run is still holding the lock.
        # Disable the timeout for this session before blocking on the lock.
        conn.execute("SET statement_timeout = 0")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        LOG.info("lance commit lock: held")
        yield
        conn.commit()
        LOG.info("lance commit lock: released (committed)")
    except BaseException:
        conn.rollback()
        LOG.warning("lance commit lock: released (rolled back)")
        raise
    finally:
        conn.close()
