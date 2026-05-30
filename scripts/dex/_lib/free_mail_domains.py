"""Free-mail domain blacklist for identity-spine bridge generators.

Used by DuckDB-on-R2 bridge scripts to filter out personal-mail domains
when building company↔company bridges (e.g. FMCSA carrier email-domain ↔
PDL company website-domain). Personal-mail domains belong in a future
person-grain bridge (`bridges/email_exact.parquet`), not in branded-domain
bridges.

The list is versioned in code so updates are auditable in git. Bump
BLACKLIST_VERSION when adding/removing entries — bridge scripts stamp
this into their ledger row + Parquet metadata so downstream consumers
can invalidate cached results when the blacklist changes.

Source list construction:
- Top-30 FMCSA carrier email domains (from operator probe 2026-05-08)
- Standard public free-mail providers (US + international consumer ISPs)
- Common ISP-bundled mail domains (sbcglobal, comcast, verizon, etc.)
"""

from __future__ import annotations

BLACKLIST_VERSION = "2026-05-09"

FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        # Top global free-mail providers
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "myyahoo.com",
        "rocketmail.com",
        "yahoo.es",
        "yahoo.co.uk",
        "yahoo.ca",
        "hotmail.com",
        "hotmail.es",
        "hotmail.co.uk",
        "hotmail.fr",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "aim.com",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "mail.ru",
        "qq.com",
        "163.com",
        "naver.com",
        # US consumer ISP mail
        "comcast.net",
        "sbcglobal.net",
        "bellsouth.net",
        "verizon.net",
        "att.net",
        "charter.net",
        "windstream.net",
        "earthlink.net",
        "cox.net",
        "frontiernet.net",
        "frontier.com",
        "optonline.net",
        "centurytel.net",
        "centurylink.net",
        "embarqmail.com",
        "tds.net",
        "juno.com",
        "hughes.net",
        "roadrunner.com",
        "netzero.net",
        "netzero.com",
        "ptd.net",
        "adelphia.net",
        "mindspring.com",
        "cableone.net",
        "netins.net",
        "fuse.net",
        "wildblue.net",
        "insightbb.com",
    }
)


def is_free_mail(domain: str | None) -> bool:
    """True if the (already-normalized) domain is on the free-mail blacklist.

    Caller is responsible for normalization (lower, trim, strip protocol /
    www. / path) — this function does NOT re-normalize. See
    `entities.match_domain_etld_plus_one` for the canonical normalization
    rule and `build_bridge_fmcsa_pdl_domain.py` for the DuckDB-side regex.

    Empty / None / non-string inputs return False (downstream filter
    treats False as "branded → keep for bridge"; the row's email column
    will catch the missing-data case separately).
    """
    if not domain or not isinstance(domain, str):
        return False
    return domain in FREE_MAIL_DOMAINS


__all__ = ["BLACKLIST_VERSION", "FREE_MAIL_DOMAINS", "is_free_mail"]
