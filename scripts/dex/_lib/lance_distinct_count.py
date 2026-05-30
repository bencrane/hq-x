"""Helper: count distinct values in a column of a Lance dataset on R2.

Used by migration-checks harness scripts to verify bridge data_source columns
have both expected provenance values present. Externalized to avoid the
quote-nesting hell of inline-python-in-bash-in-eval'd-run_surface contexts.

Usage:
    python3 lance_distinct_count.py <lance_uri> <column_name>

Prints: integer count of distinct values in the column.

Required env: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.
"""
from __future__ import annotations

import os
import sys

import lance
import pyarrow.compute as pc


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: lance_distinct_count.py <uri> <column>", file=sys.stderr)
        return 2

    uri = sys.argv[1]
    col = sys.argv[2]

    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }

    ds = lance.dataset(uri, storage_options=storage_options)
    tbl = ds.to_table(columns=[col])
    unique = pc.unique(tbl.column(col)).to_pylist()
    print(len(set(unique)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
