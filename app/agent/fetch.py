"""Fetch a URL and extract readable text — backs the `fetch_url` tool.

Two responsibilities:
- ``validate_url`` — an SSRF guard: only http(s), and (when enabled) reject
  hosts that resolve to private / loopback / link-local / reserved addresses.
- ``html_to_text`` — a dependency-free HTML → text extraction using the stdlib
  ``html.parser`` (drops script/style, keeps the <title>, collapses whitespace).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit


class FetchError(Exception):
    """Raised when a URL is unsafe or cannot be fetched."""


_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of spaces/tabs, then trim blank lines.
        lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in raw.splitlines()]
        out: list[str] = []
        for ln in lines:
            if ln or (out and out[-1]):
                out.append(ln)
        return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, text)`` extracted from an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML shouldn't break fetching
        pass
    return parser.title.strip(), parser.text()


def _is_blocked_ip(host: str) -> bool:
    """True if the host resolves to a private/loopback/link-local/reserved IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Can't resolve — treat as blocked (fail closed).
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def validate_url(url: str, *, block_private_ips: bool = True) -> str:
    """Validate a fetch target. Returns the normalized URL or raises FetchError."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"unsupported URL scheme: {parts.scheme or '(none)'!r}")
    if not parts.hostname:
        raise FetchError("URL has no host")
    if block_private_ips and _is_blocked_ip(parts.hostname):
        raise FetchError(f"refusing to fetch private/internal host: {parts.hostname}")
    return url
