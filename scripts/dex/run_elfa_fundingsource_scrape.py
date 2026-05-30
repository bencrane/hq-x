"""Scrape ELFA FundingSource directory entries, emit CSV + ZSTD Parquet, upload to R2.

Inputs:
- --urls-csv <path>: single-column CSV (header `urls`) of fundingsource detail URLs.
  Mixed numeric-ID (`/fundingsource-search/371791`) and slug
  (`/fundingsource-search/peakline-partners-llc`) forms are accepted.

Outputs:
- /tmp/elfa-cache/<sha1>.html     — raw HTML per URL (parser-replay sentinel)
- /tmp/elfa_fundingsource_<release>.csv         — operator-review CSV
- /tmp/elfa_fundingsource_<release>.parquet     — ZSTD Parquet (load-bearing)
- s3://dex-raw-landing-zone/elfa-fundingsource/release=<release>/data.parquet
                                    — R2 land; ContentType only per L42

Verify step (post-upload): DuckDB SELECT count + schema from R2 Parquet.

Per L42: ContentType=application/x-parquet, NO ContentEncoding header,
plain .parquet extension. Per L57: read-side `read_parquet` takes NO
`all_varchar`. All non-numeric columns are written as VARCHAR (pa.string)
at the Parquet write step so downstream readers get strings without flags.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import boto3
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from bs4 import BeautifulSoup, Tag


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_URLS_CSV = "/Users/benjamincrane/Downloads/elfa funderssource  - Copy of Sheet2.csv"
CACHE_DIR = Path("/tmp/elfa-cache")
OUT_DIR = Path("/tmp")
R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "elfa-fundingsource"
FETCH_CONCURRENCY = 3
FETCH_TIMEOUT = 60.0
FETCH_RETRIES = 4
FETCH_BACKOFF_BASE = 1.5
USER_AGENT = (
    "Mozilla/5.0 (compatible; data-engine-x/1.0 elfa-fundingsource-scrape; "
    "+https://substrate.build)"
)

# Field labels as they appear in <h3> on each page. Each maps to a column
# name in the output. List-typed sections render their value <div> with
# <ul><li>...</li>...</ul>; tabular sections (Transaction Size, Lease Terms)
# render two <p>-columns and have a dedicated parser.
SINGLE_VALUE_LABELS: dict[str, str] = {
    "Company Type": "company_type",
    "Type of Funding Source/Buyer": "type_of_funding_source",
    "Annual Volume Funded": "annual_volume_funded",
    "Originates Paper?": "originates_paper",
    "Syndicates/Sells Paper?": "syndicates_sells_paper",
    "Industry Financing Preferences": "industry_financing_preferences",
    "Our Company is a(n):": "our_company_is_a",
    "Accepts Soft Assets?": "accepts_soft_assets",
    "Lessor/ Broker Requirements": "lessor_broker_requirements",
    "ELFA Business Councils": "elfa_business_councils",
    "Start of the Fiscal Year": "start_of_fiscal_year",
}

LIST_VALUE_LABELS: dict[str, str] = {
    "Credit Criteria": "credit_criteria",
    "Lease Structures": "lease_structures",
    "Equipment Types We WILL Finance": "equipment_types_will_finance",
    "Equipment Types We WILL NOT Finance": "equipment_types_will_not_finance",
}

# All schema columns in stable order. Drives both the CSV header and the
# Parquet schema (every column pa.string() except where noted below).
COLUMNS = [
    "url_input",
    "entity_id_int",
    "entity_slug",
    "entity_name",
    "logo_url",
    "address",
    "primary_contact_name",
    "primary_contact_phone",
    "primary_contact_email",
    "is_a",
    "last_update",
    "last_update_date",
    "core_business_focus",
    "company_type",
    "type_of_funding_source",
    "annual_volume_funded",
    "credit_criteria",
    "transaction_size_highest",
    "transaction_size_average",
    "transaction_size_lowest",
    "lease_term_longest",
    "lease_term_average",
    "lease_term_shortest",
    "lease_structures",
    "originates_paper",
    "syndicates_sells_paper",
    "equipment_types_will_finance",
    "equipment_types_will_not_finance",
    "industry_financing_preferences",
    "our_company_is_a",
    "accepts_soft_assets",
    "lessor_broker_requirements",
    "elfa_business_councils",
    "start_of_fiscal_year",
    "scraped_at",
    "http_status",
    "raw_html_sha256",
]

INT_COLS = {"entity_id_int", "http_status"}
DATE_COLS = {"last_update_date"}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    sha256: str
    fetched_at: str


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.html"


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
) -> FetchResult:
    # Cache short-circuit: if we already have a 200-shaped cached body, skip
    # the network. Lets the parser iterate without re-hammering the source.
    cp = _cache_path(url)
    if cp.exists() and cp.stat().st_size > 5_000:
        body = cp.read_text(encoding="utf-8")
        return FetchResult(
            url=url,
            status=200,
            html=body,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    last_exc: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            async with sem:
                resp = await client.get(url, timeout=FETCH_TIMEOUT)
            body = resp.text
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            return FetchResult(
                url=url,
                status=resp.status_code,
                html=body,
                sha256=sha,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_exc = e
            backoff = FETCH_BACKOFF_BASE ** attempt
            print(f"  retry {attempt}/{FETCH_RETRIES} in {backoff:.1f}s — {url} ({type(e).__name__})", file=sys.stderr)
            await asyncio.sleep(backoff)
        except Exception as e:
            # Unexpected (e.g. malformed URL → urllib ValueError). Don't crash the loop.
            last_exc = e
            print(f"  ABORT {url} ({type(e).__name__}: {e})", file=sys.stderr)
            break
    return FetchResult(
        url=url,
        status=0,
        html=f"<!-- fetch failed: {type(last_exc).__name__}: {last_exc} -->",
        sha256="",
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def fetch_all(urls: list[str]) -> list[FetchResult]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    limits = httpx.Limits(max_keepalive_connections=FETCH_CONCURRENCY, max_connections=FETCH_CONCURRENCY)
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        http2=False,
        limits=limits,
    ) as client:
        tasks = [_fetch_one(client, sem, u) for u in urls]
        results: list[FetchResult] = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            if r.status == 200 and not _cache_path(r.url).exists():
                _cache_path(r.url).write_text(r.html, encoding="utf-8")
            results.append(r)
            marker = "OK " if r.status == 200 else f"!! {r.status} "
            print(f"  {marker}{r.url} ({len(r.html):,}B)", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

_URL_TAIL_RE = re.compile(r"/fundingsource-search/([^/?#]+)$")
_NUM_ID_RE = re.compile(r"^\d+$")
_LAST_UPDATE_RE = re.compile(r"Last Update:\s*(\d{2}/\d{2}/\d{4})")
_PHONE_RE = re.compile(r"(\(\d{3}\)\s*\d{3}-\d{4}|\d{3}[-.\s]?\d{3}[-.\s]?\d{4})")
_ADDRESS_LINE_RE = re.compile(
    r"^\d+\s|^\d+-\d+\s|^P\.?\s*O\.?\s*Box|^[A-Z][a-zA-Z\.\s]+,\s*[A-Z]{2}(\s+\d{5}(-\d{4})?)?$",
    re.IGNORECASE,
)


def _decode_cfemail(hex_str: str) -> str:
    """Decode Cloudflare email-protection hex string.

    Format: first byte is XOR key; remaining bytes XOR'd against the key
    give ASCII chars of the email address.
    """
    try:
        b = bytes.fromhex(hex_str)
    except ValueError:
        return ""
    if len(b) < 2:
        return ""
    key = b[0]
    return "".join(chr(c ^ key) for c in b[1:])


def _split_url_tail(url: str) -> tuple[int | None, str | None]:
    m = _URL_TAIL_RE.search(url.strip())
    if not m:
        return None, None
    tail = m.group(1)
    if _NUM_ID_RE.match(tail):
        return int(tail), None
    return None, tail


def _h3_value_div(soup: BeautifulSoup, label_text: str) -> Tag | None:
    """Find the <h3> whose stripped text == label_text and return the value <div>.

    Value div is either:
      - the immediate next sibling <div>, OR
      - if the parent is a flex row, the second <div> child of the parent that
        is not the <h3>'s wrapper.
    """
    for h3 in soup.find_all("h3"):
        if h3.get_text(strip=True) != label_text:
            continue
        # Strategy 1: next-sibling div
        sib = h3.find_next_sibling("div")
        if sib is not None:
            return sib
        # Strategy 2: walk up to parent, take second div child
        parent = h3.parent
        if parent is not None:
            divs = [c for c in parent.find_all("div", recursive=False)]
            if divs:
                return divs[-1]
    return None


def _text_from_value_div(div: Tag | None) -> str:
    if div is None:
        return ""
    ul = div.find("ul")
    if ul is not None:
        items = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
        return "|".join(items)
    return div.get_text(" ", strip=True)


def _parse_transaction_or_term(soup: BeautifulSoup, label: str) -> tuple[str, str, str]:
    """Tabular two-column section (Transaction Size / Lease Terms).

    Returns (top, mid, bottom) — corresponds to (Highest/Longest, Average,
    Lowest/Shortest).
    """
    for h3 in soup.find_all("h3"):
        if h3.get_text(strip=True) != label:
            continue
        parent = h3.parent
        if parent is None:
            continue
        # value column lives in a sibling div with two inner div columns
        value_wrapper = h3.find_next_sibling("div")
        if value_wrapper is None:
            continue
        inner_divs = value_wrapper.find_all("div", recursive=False)
        # Need the rightmost column (values) — labels are in the first.
        if len(inner_divs) >= 2:
            value_col = inner_divs[-1]
            ps = [p.get_text(" ", strip=True) for p in value_col.find_all("p")]
            top = ps[0] if len(ps) > 0 else ""
            mid = ps[1] if len(ps) > 1 else ""
            bot = ps[2] if len(ps) > 2 else ""
            return top, mid, bot
    return "", "", ""


def _decode_cf_protected_emails(soup: BeautifulSoup) -> list[str]:
    """Decode every `data-cfemail="..."` tag in the document.

    Replaces the tag's text with the plaintext email and returns the list of
    decoded emails in document order. The actual ELFA markup nests the
    `data-cfemail` on a `<span>` inside an `<a href="/cdn-cgi/l/email-protection#..."`>
    (not a `mailto:`), so callers should rely on this returned list instead of
    searching for `<a href="mailto:...">`.
    """
    decoded_all: list[str] = []
    for el in soup.find_all(attrs={"data-cfemail": True}):
        decoded = _decode_cfemail(el["data-cfemail"])
        if decoded:
            el.string = decoded
            decoded_all.append(decoded)
    return decoded_all


def _parse_company_info_block(soup: BeautifulSoup, decoded_emails: list[str]) -> dict[str, str]:
    """Pull entity name, logo URL, address, primary contact (name/phone/email),
    is_a, last_update from the top Company Info section."""
    out: dict[str, str] = {
        "entity_name": "",
        "logo_url": "",
        "address": "",
        "primary_contact_name": "",
        "primary_contact_phone": "",
        "primary_contact_email": "",
        "is_a": "",
        "last_update": "",
        "last_update_date": "",
    }

    # entity_name: <title>
    title = soup.find("title")
    if title:
        out["entity_name"] = title.get_text(strip=True)

    # Resolve the Company Info section by finding the `<!-- Company Info Section -->`
    # HTML comment and taking the next `<section>` after it. This is more reliable
    # than the logo-img heuristic: some entities don't have a custom logo, so the
    # "Site Logo" image in the header would be mistaken for the entity block.
    from bs4 import Comment
    addr_section = None
    for c in soup.find_all(string=lambda s: isinstance(s, Comment) and "Company Info Section" in s):
        sec = c.find_next("section")
        if sec is not None:
            addr_section = sec
            break

    # logo URL: the <img> with alt ending "Logo" in the Company Info section.
    if addr_section is not None:
        for img in addr_section.find_all("img"):
            alt = img.get("alt", "") or ""
            if alt.endswith("Logo"):
                src = img.get("src", "") or ""
                if src.startswith("/"):
                    src = "https://www.elfaonline.org" + src
                out["logo_url"] = src
                break

    # email: first Cloudflare-decoded address (markup uses
    # `<a href="/cdn-cgi/l/email-protection#..."><span data-cfemail="..."></span></a>`,
    # not `mailto:`). Fall back to mailto: anchor if a future page uses plain links.
    if decoded_emails:
        out["primary_contact_email"] = decoded_emails[0]
    else:
        a_mail = soup.find("a", href=lambda v: isinstance(v, str) and v.startswith("mailto:"))
        if a_mail:
            out["primary_contact_email"] = a_mail["href"].removeprefix("mailto:").strip()

    # is_a: structure is `<span class="font-bold">{entity_name} is a: </span><span>{value}</span>`
    for span in soup.find_all("span", class_="font-bold"):
        txt = span.get_text(" ", strip=True)
        if txt.endswith("is a:"):
            nxt = span.find_next_sibling("span")
            if nxt is not None:
                out["is_a"] = nxt.get_text(" ", strip=True)
            break

    # Primary Contact name + phone (and address) inside the Company Info section.
    if addr_section is not None:
        # phone: tel: anchor anywhere under the section
        a_tel = addr_section.find("a", href=lambda v: isinstance(v, str) and v.startswith("tel:"))
        if a_tel:
            out["primary_contact_phone"] = (
                a_tel.get_text(" ", strip=True)
                or a_tel["href"].removeprefix("tel:")
            )

        # Primary contact name: element immediately preceding the "Primary Contact"
        # subtitle within the same column block (the page uses <h3> for the name,
        # not <div>, so do an unfiltered previous_sibling walk).
        for el in addr_section.find_all(string=lambda s: isinstance(s, str) and s.strip() == "Primary Contact"):
            cur = el.parent  # <div>Primary Contact</div>
            prev = cur.find_previous_sibling()
            while prev is not None:
                name_txt = prev.get_text(" ", strip=True)
                if name_txt and not name_txt.endswith("Logo") and "is a:" not in name_txt and name_txt != "Primary Contact":
                    out["primary_contact_name"] = name_txt
                    break
                prev = prev.find_previous_sibling()
            break

        # Address: text rows in the Company Info section that look like address
        # lines (street with leading number / city, ST ZIP / PO Box). Drops
        # entity name, contact name, phone, email, "Primary Contact" / "Alternate
        # Contact" labels, "Last Update", and the "is a:" prefix.
        all_lines = [
            ln.strip()
            for ln in addr_section.get_text("\n").splitlines()
            if ln.strip()
        ]
        drop = {
            out["entity_name"],
            out["primary_contact_name"],
            out["primary_contact_phone"],
            out["primary_contact_email"],
            "Primary Contact",
            "Alternate Contact",
            "Company Info",
        }
        addr_lines: list[str] = []
        for ln in all_lines:
            if ln in drop:
                continue
            if ln.startswith("Last Update"):
                continue
            if "is a:" in ln:
                continue
            # Stop once we hit a second contact block — addresses come before
            # contact names in the layout, so the first contact-shaped line ends
            # the address.
            if ln == out["primary_contact_name"]:
                break
            if not _ADDRESS_LINE_RE.search(ln):
                continue
            addr_lines.append(ln)
        out["address"] = "\n".join(addr_lines)

    # last_update
    txt = soup.get_text(" ", strip=False)
    m = _LAST_UPDATE_RE.search(txt)
    if m:
        out["last_update"] = m.group(1)
        try:
            d = datetime.strptime(m.group(1), "%m/%d/%Y").date()
            out["last_update_date"] = d.isoformat()
        except ValueError:
            pass
    return out


def _parse_core_business_focus(soup: BeautifulSoup) -> str:
    """Find the <h2>Core Business Focus</h2> and return the next sibling <div>'s text."""
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True) == "Core Business Focus":
            sib = h2.find_next_sibling("div")
            if sib is not None:
                return sib.get_text(" ", strip=True)
    return ""


def parse_page(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    decoded_emails = _decode_cf_protected_emails(soup)
    row: dict[str, str] = {c: "" for c in COLUMNS}

    info = _parse_company_info_block(soup, decoded_emails)
    row.update({k: v for k, v in info.items() if k in row})

    row["core_business_focus"] = _parse_core_business_focus(soup)

    for label, col in SINGLE_VALUE_LABELS.items():
        row[col] = _text_from_value_div(_h3_value_div(soup, label))

    for label, col in LIST_VALUE_LABELS.items():
        row[col] = _text_from_value_div(_h3_value_div(soup, label))

    top, mid, bot = _parse_transaction_or_term(soup, "Transaction Size")
    row["transaction_size_highest"] = top
    row["transaction_size_average"] = mid
    row["transaction_size_lowest"] = bot

    top, mid, bot = _parse_transaction_or_term(soup, "Lease Terms")
    row["lease_term_longest"] = top
    row["lease_term_average"] = mid
    row["lease_term_shortest"] = bot

    return row


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def build_row(fetch: FetchResult) -> dict[str, str]:
    parsed = parse_page(fetch.html)
    eid, slug = _split_url_tail(fetch.url)
    parsed["url_input"] = fetch.url
    parsed["entity_id_int"] = "" if eid is None else str(eid)
    parsed["entity_slug"] = slug or ""
    parsed["scraped_at"] = fetch.fetched_at
    parsed["http_status"] = str(fetch.status)
    parsed["raw_html_sha256"] = fetch.sha256
    return parsed


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})


def _to_arrow_table(rows: list[dict[str, str]]) -> pa.Table:
    # All non-int/non-date cols stay as string (pa.string()); see L57 — values
    # baked into VARCHAR at write side so downstream `read_parquet` needs no flag.
    fields: list[pa.Field] = []
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    for c in COLUMNS:
        if c in INT_COLS:
            fields.append(pa.field(c, pa.int64()))
        elif c in DATE_COLS:
            fields.append(pa.field(c, pa.date32()))
        else:
            fields.append(pa.field(c, pa.string()))

    for r in rows:
        for c in COLUMNS:
            v = r.get(c, "")
            if c in INT_COLS:
                cols[c].append(int(v) if v not in ("", None) else None)
            elif c in DATE_COLS:
                if v:
                    try:
                        cols[c].append(date.fromisoformat(v))
                    except ValueError:
                        cols[c].append(None)
                else:
                    cols[c].append(None)
            else:
                cols[c].append(v if v is not None else "")
    schema = pa.schema(fields)
    arrays = [pa.array(cols[c], type=schema.field(c).type) for c in COLUMNS]
    return pa.Table.from_arrays(arrays, schema=schema)


def write_parquet(rows: list[dict[str, str]], path: Path) -> pa.Table:
    table = _to_arrow_table(rows)
    pq.write_table(
        table,
        path,
        compression="zstd",
        row_group_size=100_000,
    )
    return table


# ---------------------------------------------------------------------------
# Upload + verify
# ---------------------------------------------------------------------------

def upload_r2(parquet_path: Path, release: str) -> str:
    """Upload Parquet to R2. ContentType only; NO ContentEncoding per L42.

    Returns the s3:// URI.
    """
    key = f"{R2_PREFIX}/release={release}/data.parquet"
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    s3.upload_file(
        str(parquet_path),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return f"s3://{R2_BUCKET}/{key}"


def verify_r2_duckdb(r2_uri: str) -> dict[str, object]:
    """Verify the R2 Parquet by downloading via boto3 to /tmp/ and reading
    locally with DuckDB. Avoids the L58 R2-routing-via-httpfs gotcha
    (HTTP 404 from `read_parquet('s3://...')` even when boto3 reaches the file).
    """
    import duckdb  # local import — only needed in verify path
    bucket, _, key = r2_uri.removeprefix("s3://").partition("/")
    local = Path(f"/tmp/elfa_fundingsource_verify_{hashlib.sha1(r2_uri.encode()).hexdigest()[:12]}.parquet")
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    s3.download_file(bucket, key, str(local))
    con = duckdb.connect()
    total = con.execute(f"SELECT count(*) FROM read_parquet('{local}')").fetchone()[0]
    ok = con.execute(
        f"SELECT count(*) FROM read_parquet('{local}') WHERE http_status = 200"
    ).fetchone()[0]
    with_name = con.execute(
        f"SELECT count(*) FROM read_parquet('{local}') WHERE entity_name != ''"
    ).fetchone()[0]
    schema_rows = con.execute(
        f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM read_parquet('{local}'))"
    ).fetchall()
    sample = con.execute(
        f"SELECT entity_name, company_type, annual_volume_funded, last_update_date "
        f"FROM read_parquet('{local}') ORDER BY entity_name LIMIT 5"
    ).fetchall()
    return {
        "total": total,
        "http_200": ok,
        "with_entity_name": with_name,
        "schema": schema_rows,
        "sample": sample,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_URL_TAIL_NONEMPTY_RE = re.compile(r"/fundingsource-search/[^/?#]+/?$")


def read_urls(csv_path: Path) -> list[str]:
    """Tolerant: skip header, strip surrounding quotes/whitespace, dedup,
    drop URLs that don't have a non-empty `/fundingsource-search/<tail>` path
    (e.g. the search-root URL with no entity).
    """
    rows: list[str] = []
    seen: set[str] = set()
    with csv_path.open(newline="", encoding="utf-8") as f:
        for i, line in enumerate(f):
            v = line.strip().strip('"').strip("'").strip()
            if not v:
                continue
            if i == 0 and v.lower() == "urls":
                continue
            if not _URL_TAIL_NONEMPTY_RE.search(v):
                print(f"  skip (no entity tail): {v!r}", file=sys.stderr)
                continue
            v = v.rstrip("/")
            if v in seen:
                continue
            seen.add(v)
            rows.append(v)
    return rows


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--urls-csv", default=DEFAULT_URLS_CSV)
    p.add_argument("--release", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--no-upload", action="store_true", help="Skip R2 upload + verify.")
    args = p.parse_args(list(argv) if argv is not None else None)

    urls_csv = Path(args.urls_csv)
    if not urls_csv.exists():
        print(f"FATAL: --urls-csv not found: {urls_csv}", file=sys.stderr)
        return 2

    release = args.release
    urls = read_urls(urls_csv)
    print(f"Read {len(urls)} URLs from {urls_csv}", file=sys.stderr)

    print(f"Fetching (concurrency={FETCH_CONCURRENCY}) ...", file=sys.stderr)
    fetches = asyncio.run(fetch_all(urls))
    fetches.sort(key=lambda r: urls.index(r.url))

    bad = [r for r in fetches if r.status != 200]
    if bad:
        print(f"WARN: {len(bad)} non-200 responses:", file=sys.stderr)
        for r in bad:
            print(f"  {r.status} {r.url}", file=sys.stderr)

    print("Parsing ...", file=sys.stderr)
    rows: list[dict[str, str]] = []
    for fr in fetches:
        rows.append(build_row(fr))

    csv_out = OUT_DIR / f"elfa_fundingsource_{release}.csv"
    parquet_out = OUT_DIR / f"elfa_fundingsource_{release}.parquet"
    write_csv(rows, csv_out)
    table = write_parquet(rows, parquet_out)
    print(
        f"Wrote {len(rows)} rows: {csv_out} ({csv_out.stat().st_size:,}B), "
        f"{parquet_out} ({parquet_out.stat().st_size:,}B; "
        f"{table.num_columns} cols)",
        file=sys.stderr,
    )

    if args.no_upload:
        return 0

    print(f"Uploading to R2 ({R2_BUCKET}/{R2_PREFIX}/release={release}/) ...", file=sys.stderr)
    r2_uri = upload_r2(parquet_out, release)
    print(f"  {r2_uri}", file=sys.stderr)

    print("Verifying via DuckDB on R2 ...", file=sys.stderr)
    summary = verify_r2_duckdb(r2_uri)
    print(f"  rows={summary['total']}  http_200={summary['http_200']}  with_name={summary['with_entity_name']}", file=sys.stderr)
    print("  sample (first 5 by name):", file=sys.stderr)
    for s in summary["sample"]:
        print(f"    {s}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
