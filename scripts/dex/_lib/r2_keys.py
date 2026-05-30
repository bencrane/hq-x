"""R2 key existence + non-empty probe.

Idempotency idiom: scripts call `r2_object_is_landed(...)` to decide whether
to skip work. Returns True ONLY when an object exists AND has ContentLength > 0.

A 0-byte object is treated as nonexistent (i.e., `r2_object_is_landed` returns
False) because the writer poison-file class of bug (see modal/landing/r2.py)
historically left 0-byte objects in R2 that idempotency caches treated as
"already done." This caused real reruns to no-op. Defense-in-depth pair with
R2Landing fix in r2.py.

Cycle: usaspending-poison-file-class-fix-v1 (2026-05-13)
"""

from __future__ import annotations


def r2_object_is_landed(s3, *, bucket: str, key: str) -> bool:
    """True iff a non-empty object exists at bucket/key. 0-byte = False.

    Replaces the pre-fix `_r2_key_exists` idiom used across 16 idempotency-gate
    sites in DEX ingest scripts. The key behavioral change: a HEAD-200 response
    with ContentLength=0 now returns False, preventing poison files from locking
    future reruns.

    Usage::

        from scripts._lib.r2_keys import r2_object_is_landed

        if r2_object_is_landed(s3_client, bucket="dex-raw-landing-zone", key=output_key):
            print(f"Already landed, skipping: {output_key}")
            return

    OUT-OF-SCOPE sites (metadata probes for logging, byte-counting, audit scripts)
    should NOT use this helper — they need the raw HEAD response, not a boolean.
    Only idempotency-gate sites (output-key skip gates) should migrate to this helper.
    """
    from botocore.exceptions import ClientError

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return int(head.get("ContentLength", 0)) > 0
