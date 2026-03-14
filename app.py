#!/usr/bin/env python3
"""
Mirror proxy for https://www.cubeskills.com/

Features
--------
* Strips ``X-Frame-Options``, ``Content-Security-Policy`` and related headers
  from every upstream response so the site can be embedded inside an <iframe>.
* Rewrites absolute URLs (href / src / action / CSS url() / inline JS strings)
  that point to the target origin so they continue to route through this proxy.
* Local disk cache with a configurable size limit (default 512 MB).  The cache
  uses LRU eviction, so the oldest entries are dropped when the limit is hit.

Configuration (environment variables)
--------------------------------------
CACHE_DIR       Directory used for the disk cache.   Default: ``./cache``
CACHE_SIZE_MB   Maximum cache size in megabytes.      Default: ``512``
PORT            TCP port the proxy listens on.        Default: ``5000``
HOST            Bind address.                         Default: ``0.0.0.0``
REQUEST_TIMEOUT Upstream request timeout in seconds.  Default: ``30``

Usage
-----
::

    pip install -r requirements.txt
    python app.py

Then open http://localhost:5000/ in your browser, or embed it in an <iframe>.
"""

import hashlib
import html as html_module
import logging
import os
import re
from urllib.parse import urlparse

import diskcache
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_URL = "https://www.cubeskills.com"
TARGET_HOST = urlparse(TARGET_URL).netloc  # "www.cubeskills.com"

CACHE_DIR = os.environ.get("CACHE_DIR", "./cache")
CACHE_SIZE_MB = int(os.environ.get("CACHE_SIZE_MB", "512"))
CACHE_SIZE_LIMIT = CACHE_SIZE_MB * 1024 * 1024  # bytes

PORT = int(os.environ.get("PORT", "5000"))
HOST = os.environ.get("HOST", "0.0.0.0")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Disk cache (created once at module level)
# ---------------------------------------------------------------------------

os.makedirs(CACHE_DIR, exist_ok=True)
cache: diskcache.Cache = diskcache.Cache(CACHE_DIR, size_limit=CACHE_SIZE_LIMIT)

# ---------------------------------------------------------------------------
# Header filter lists
# ---------------------------------------------------------------------------

# Response headers from upstream that must NOT be forwarded to the client.
# transfer-encoding / content-encoding are excluded because requests already
# decompresses the body for us, so forwarding them would be wrong.
STRIP_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "x-frame-options",
        "content-security-policy",
        "x-content-security-policy",
        "content-security-policy-report-only",
        "strict-transport-security",
        "transfer-encoding",
        "content-encoding",
        "connection",
    }
)

# Request headers from the client that we must not forward upstream verbatim.
STRIP_REQUEST_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "accept-encoding",  # requests sets this itself
    }
)

# ---------------------------------------------------------------------------
# URL-rewriting helpers
# ---------------------------------------------------------------------------


def rewrite_url(url: str) -> str:
    """Return a proxy-relative path when *url* points to the mirrored site.

    Protocol-relative URLs (``//www.cubeskills.com/...``) are normalised to
    ``https`` before the host check.  All other URLs are returned unchanged.
    """
    if not url:
        return url
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc == TARGET_HOST:
        result = parsed.path or "/"
        if parsed.query:
            result += "?" + parsed.query
        if parsed.fragment:
            result += "#" + parsed.fragment
        return result
    return url


def rewrite_html(content: bytes) -> bytes:
    """Rewrite target-host URLs inside HTML and remove framing meta-headers."""
    try:
        soup = BeautifulSoup(content, "lxml")

        # Remove <meta http-equiv="X-Frame-Options|Content-Security-Policy">
        for meta in soup.find_all("meta", attrs={"http-equiv": True}):
            if meta["http-equiv"].lower() in (
                "x-frame-options",
                "content-security-policy",
            ):
                meta.decompose()

        # Rewrite common URL-bearing attributes
        for tag in soup.find_all(True):
            for attr in ("href", "src", "action", "data"):
                val = tag.get(attr)
                if val and isinstance(val, str):
                    tag[attr] = rewrite_url(val)

            # srcset="url descriptor, url descriptor, ..."
            srcset = tag.get("srcset")
            if srcset and isinstance(srcset, str):
                parts = []
                for part in srcset.split(","):
                    tokens = part.strip().split()
                    if tokens:
                        tokens[0] = rewrite_url(tokens[0])
                    parts.append(" ".join(tokens))
                tag["srcset"] = ", ".join(parts)

        return soup.encode("utf-8")
    except (ValueError, AttributeError) as exc:  # pragma: no cover
        logger.warning("HTML rewrite failed: %s", exc)
        return content


def rewrite_css(content: bytes) -> bytes:
    """Rewrite target-host URLs inside CSS ``url()`` expressions."""
    try:
        text = content.decode("utf-8", errors="replace")

        def _replace(match: re.Match) -> str:
            inner = match.group(1).strip()
            quote = ""
            if inner and inner[0] in ('"', "'"):
                quote = inner[0]
                inner = inner[1:-1]
            rewritten = rewrite_url(inner)
            return f"url({quote}{rewritten}{quote})"

        text = re.sub(r"url\(([^)]+)\)", _replace, text)
        return text.encode("utf-8")
    except (UnicodeDecodeError, re.error) as exc:  # pragma: no cover
        logger.warning("CSS rewrite failed: %s", exc)
        return content


def rewrite_js(content: bytes) -> bytes:
    """Rewrite hard-coded target-host URLs inside JavaScript files."""
    try:
        text = content.decode("utf-8", errors="replace")
        pattern = re.compile(
            r'(["\'])https?://' + re.escape(TARGET_HOST) + r'(/[^"\']*?)(\1)'
        )
        text = pattern.sub(lambda m: m.group(1) + m.group(2) + m.group(3), text)
        return text.encode("utf-8")
    except (UnicodeDecodeError, re.error) as exc:  # pragma: no cover
        logger.warning("JS rewrite failed: %s", exc)
        return content


# ---------------------------------------------------------------------------
# Core proxy logic
# ---------------------------------------------------------------------------


def _cache_key(path: str, query: str) -> str:
    """SHA-256 hex digest used as the diskcache key."""
    raw = f"{path}?{query}" if query else path
    return hashlib.sha256(raw.encode()).hexdigest()


def _build_upstream_headers() -> dict:
    """Return request headers to send to the upstream site."""
    headers: dict[str, str] = {}
    for name, value in request.headers:
        if name.lower() not in STRIP_REQUEST_HEADERS:
            headers[name] = value
    headers["Host"] = TARGET_HOST
    return headers


def _fetch_upstream(path: str, query: str) -> requests.Response:
    url = TARGET_URL + path
    if query:
        url += "?" + query
    return requests.request(
        method=request.method,
        url=url,
        headers=_build_upstream_headers(),
        data=request.get_data() if request.method in ("POST", "PUT", "PATCH") else None,
        cookies=request.cookies,
        allow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )


def _build_response(upstream: requests.Response) -> Response:
    """Build a Flask ``Response`` from an upstream ``requests.Response``."""
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    body = upstream.content

    ct_lower = content_type.lower()
    if "text/html" in ct_lower:
        body = rewrite_html(body)
    elif "text/css" in ct_lower:
        body = rewrite_css(body)
    elif "javascript" in ct_lower or "text/js" in ct_lower:
        body = rewrite_js(body)

    # Build outbound headers: drop security-related ones, rewrite Location
    out_headers: dict[str, str] = {}
    for name, value in upstream.headers.items():
        if name.lower() in STRIP_RESPONSE_HEADERS:
            continue
        if name.lower() == "location":
            value = rewrite_url(value)
        out_headers[name] = value

    return Response(
        body,
        status=upstream.status_code,
        headers=out_headers,
        content_type=content_type,
    )


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@app.route("/", defaults={"path": ""}, methods=_ALL_METHODS)
@app.route("/<path:path>", methods=_ALL_METHODS)
def proxy(path: str) -> Response:
    """Catch-all handler that forwards every request to the upstream site."""
    full_path = "/" + path
    query = request.query_string.decode("utf-8")

    # Only cache idempotent GET requests
    use_cache = request.method == "GET"
    key = _cache_key(full_path, query) if use_cache else None

    if use_cache and key in cache:
        logger.info("HIT  %s", full_path)
        cached = cache[key]
        return Response(
            cached["body"],
            status=cached["status"],
            headers=cached["headers"],
            content_type=cached["content_type"],
        )

    logger.info("MISS %s", full_path)

    try:
        upstream = _fetch_upstream(full_path, query)
    except requests.RequestException as exc:
        logger.error("Upstream error for %s: %s", full_path, exc)
        safe_msg = html_module.escape(str(exc))
        return Response(
            f"<h1>Proxy Error</h1><p>{safe_msg}</p>",
            status=502,
            content_type="text/html",
        )

    resp = _build_response(upstream)

    # Persist successful GET responses in the disk cache.
    # diskcache enforces the size limit automatically (LRU eviction).
    if use_cache and upstream.status_code == 200:
        cache[key] = {
            "body": resp.get_data(),
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "content_type": resp.content_type,
        }

    return resp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting mirror proxy → %s", TARGET_URL)
    logger.info(
        "Disk cache: %s  (limit: %d MB)",
        os.path.abspath(CACHE_DIR),
        CACHE_SIZE_MB,
    )
    logger.info("Listening on http://%s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False)
