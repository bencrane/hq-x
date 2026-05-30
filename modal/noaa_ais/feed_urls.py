"""NOAA MarineCadastre archive URL conventions.

NOAA publishes one zipped CSV per UTC day under:
    https://coast.noaa.gov/htdata/CMSP/AISDataHandler/{YYYY}/AIS_{YYYY}_{MM}_{DD}.zip

The zip contains a single CSV with header row matching the columns we mirror in
entities.source_ais_pings. Years 2018+ use the canonical 17-column schema;
older years (2015-2017) used different column orderings — out of scope for v0.

Publication lag is 6-12 months — operator should target dates with available files.
"""

from __future__ import annotations

from datetime import date

ARCHIVE_ROOT = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler"
EARLIEST_SUPPORTED_YEAR = 2018


def daily_csv_url(feed_date: date) -> str:
    if feed_date.year < EARLIEST_SUPPORTED_YEAR:
        raise ValueError(
            f"feed_date={feed_date.isoformat()} pre-dates the canonical 17-column "
            f"schema (earliest supported: {EARLIEST_SUPPORTED_YEAR}-01-01)"
        )
    return (
        f"{ARCHIVE_ROOT}/{feed_date.year}/"
        f"AIS_{feed_date.year}_{feed_date.month:02d}_{feed_date.day:02d}.zip"
    )


def daily_zip_filename(feed_date: date) -> str:
    return f"AIS_{feed_date.year}_{feed_date.month:02d}_{feed_date.day:02d}.zip"


def daily_csv_filename_inside_zip(feed_date: date) -> str:
    return f"AIS_{feed_date.year}_{feed_date.month:02d}_{feed_date.day:02d}.csv"
