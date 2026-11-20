"""Scraper configuration. Values are project choices and are recorded in data/manifest.json."""

from __future__ import annotations

SCRAPER_VERSION = "1.0.0"
CORPUS_SCHEMA_VERSION = 1

# Honest, generic identity: no personal data and no impersonation of a browser or of
# another company's crawler (Wasalt's robots.txt grants extra access only to named AI bots).
USER_AGENT = "DGW-AssignmentScraper/1.0 (candidate technical assignment; polite; robots-aware)"

# The only hosts the scraper may contact: exact host or a subdomain of it, https only.
ALLOWED_HOSTS: dict[str, str] = {
    "darglobal.co.uk": "darglobal",
    "wasalt.sa": "wasalt",
    "web.archive.org": "archive",
}

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 45.0
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_SITEMAP_BYTES = 40 * 1024 * 1024  # Wasalt's listing sitemap is ~10 MB uncompressed
MAX_CDX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5

DEFAULT_MIN_INTERVAL_S = 2.0
MIN_INTERVAL_S: dict[str, float] = {
    "darglobal.co.uk": 2.0,
    "www.darglobal.co.uk": 2.0,
    "wasalt.sa": 2.0,
    "cdn.wasalt.sa": 1.0,
    "web.archive.org": 3.0,
}
MAX_ATTEMPTS = 4  # per request; only transient failures (timeouts, 429, 5xx) are retried
BACKOFF_BASE_S = 4.0
BACKOFF_CAP_S = 60.0

MAX_DARGLOBAL_PAGES = 450
MAX_CAPTURE_TRIES = 3  # older captures tried when the newest archived copy is a challenge stub
MIN_CAPTURE_LENGTH = 1500  # CDX record length below this is almost always a challenge stub
ARCHIVE_FROM = "2025"  # only Internet Archive captures from 2025 onward
MAX_DOC_CHARS = 20_000
MAX_LINE_CHARS = 2_000

DARGLOBAL_SKIP_PREFIXES = (
    "/pay-online",
    "/bookyourunit",
    "/landingpage",
    "/internship-application",
    "/new-home-page",
)

# Wasalt pages looked up individually in the Internet Archive (newest real capture wins).
WASALT_ARCHIVE_TARGETS: tuple[str, ...] = (
    "https://wasalt.sa/en/",
    "https://wasalt.sa/",
    "https://wasalt.sa/en/s/wasalt-business",
    "https://wasalt.sa/en/s/masterplan",
    "https://wasalt.sa/en/s/terms-of-advertising",
    "https://wasalt.sa/en/s/terms-of-use",
    "https://wasalt.sa/en/s/privacy-policy",
    "https://wasalt.sa/en/properties-for-sale-in-riyadh",
    "https://wasalt.sa/en/properties-for-sale-in-jeddah",
    "https://wasalt.sa/en/properties-for-sale-in-dammam",
    "https://wasalt.sa/en/properties-for-sale-in-khobar",
    "https://wasalt.sa/en/properties-for-sale-in-makkah",
    "https://wasalt.sa/en/properties-for-sale-in-madinah",
    "https://wasalt.sa/en/properties-for-rent-in-riyadh",
    "https://wasalt.sa/en/properties-for-rent-in-jeddah",
    "https://wasalt.sa/en/properties-for-rent-in-dammam",
    "https://wasalt.sa/عقارات-للبيع-في-الرياض",
    "https://wasalt.sa/عقارات-للبيع-في-جدة",
    "https://wasalt.sa/عقارات-للبيع-في-الدمام",
    "https://wasalt.sa/عقارات-للايجار-في-الرياض",
    "https://wasalt.sa/عقارات-للايجار-في-جدة",
)
