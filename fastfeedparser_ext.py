from __future__ import annotations

import gzip
import os
import random
import socket
import time
import zlib
from typing import Union
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPErrorProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from lxml import etree

from fastfeedparser.main import FastFeedParserDict, _parse_feed_entry, _parse_feed_info

DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("FASTFEEDPARSER_TIMEOUT", "30"))
DEFAULT_RETRIES = int(os.environ.get("FASTFEEDPARSER_RETRIES", "3"))
DEFAULT_BACKOFF_INITIAL = float(os.environ.get("FASTFEEDPARSER_BACKOFF_INITIAL", "1.0"))
DEFAULT_BACKOFF_MAX = float(os.environ.get("FASTFEEDPARSER_BACKOFF_MAX", "10.0"))


def _collect_categories(item, feed_type):
    categories = []
    if feed_type in {"rss", "rdf"}:
        for cat in item.findall("category"):
            if cat.text:
                value = cat.text.strip()
                if value:
                    categories.append(value)
        for cat in item.findall("{http://purl.org/dc/elements/1.1/}subject"):
            if cat.text:
                value = cat.text.strip()
                if value:
                    categories.append(value)
    elif feed_type == "atom":
        for cat in item.findall("{http://www.w3.org/2005/Atom}category"):
            term = cat.get("term") or cat.get("label")
            if not term and cat.text:
                term = cat.text.strip()
            if term:
                categories.append(term.strip())

    # De-duplicate while preserving order
    seen = set()
    deduped = []
    for value in categories:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _decode_http_body(content: bytes, content_encoding: str | None) -> bytes:
    """Decode gzip/deflate if needed; otherwise return content."""
    if not content:
        return content

    enc = (content_encoding or "").lower().strip()
    if enc == "gzip":
        return gzip.decompress(content)

    if enc == "deflate":
        # Some servers send raw DEFLATE (RFC1951), others zlib-wrapped (RFC1950).
        # Try raw first, then zlib-wrapped.
        try:
            return zlib.decompress(content, -zlib.MAX_WBITS)
        except zlib.error:
            return zlib.decompress(content)

    return content


def _fetch_url(source: str, timeout: float, retries: int) -> Union[str, bytes]:
    """Fetch feed URL with retries. Returns decoded text (str) if charset known, otherwise bytes."""
    request = Request(
        source,
        method="GET",
        headers={
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "fastfeedparser (+https://github.com/kagisearch/fastfeedparser)",
        },
    )
    opener = build_opener(HTTPRedirectHandler(), HTTPErrorProcessor())

    backoff = DEFAULT_BACKOFF_INITIAL
    last_exc: Exception | None = None

    for attempt in range(1, max(1, retries) + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                # response.begin() isn't necessary; urllib handles it internally.
                content: bytes = response.read()

                content = _decode_http_body(content, response.headers.get("Content-Encoding"))
                charset = response.headers.get_content_charset()

                if charset:
                    return content.decode(charset, errors="replace")
                return content

        except HTTPError as e:
            # HTTPError is also a file-like object, but for feeds we usually want to fail fast on 4xx
            # and retry on 5xx/429.
            last_exc = e
            status = getattr(e, "code", None)
            if status is not None and (status == 429 or 500 <= status <= 599):
                # retryable
                pass
            else:
                raise ValueError(f"HTTP error fetching feed ({status}): {e.reason}") from e

        except (TimeoutError, socket.timeout) as e:
            last_exc = e

        except URLError as e:
            last_exc = e

        # Retry path
        if attempt < retries:
            # exponential backoff with jitter
            sleep_for = min(backoff, DEFAULT_BACKOFF_MAX) * (0.7 + random.random() * 0.6)
            time.sleep(sleep_for)
            backoff *= 2

    # Out of retries
    if isinstance(last_exc, (TimeoutError, socket.timeout)):
        raise TimeoutError(f"Timed out fetching feed after {retries} attempts: {source}") from last_exc
    raise ConnectionError(f"Failed to fetch feed after {retries} attempts: {source}") from last_exc


def parse(
    source: str | bytes,
    *,
    timeout: float | None = None,
    retries: int | None = None,
) -> FastFeedParserDict:
    """
    Parse a feed from a URL or XML content, with category extraction.

    - If source is a URL, fetch it with retries and decode gzip/deflate.
    - timeout: per-attempt socket timeout in seconds (default from FASTFEEDPARSER_TIMEOUT or 30)
    - retries: number of attempts (default from FASTFEEDPARSER_RETRIES or 3)
    """
    timeout_val = float(DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout)
    retries_val = int(DEFAULT_RETRIES if retries is None else retries)

    if isinstance(source, str) and source.startswith(("http://", "https://")):
        xml_content = _fetch_url(source, timeout=timeout_val, retries=retries_val)
    else:
        xml_content = source

    if isinstance(xml_content, str):
        xml_content = xml_content.encode("utf-8", errors="replace")

    if not xml_content.strip():
        raise ValueError("Empty content")

    parser = etree.XMLParser(
        ns_clean=True,
        recover=True,
        collect_ids=False,
        resolve_entities=False,
    )

    try:
        root = etree.fromstring(xml_content, parser=parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Failed to parse XML content: {str(e)}") from e

    if root is None:
        raise ValueError("Failed to parse XML content: root element is None")

    if root.tag == "rss" or root.tag.endswith("}rss"):
        feed_type = "rss"
        channel = root.find("channel")
        if channel is None:
            raise ValueError("Invalid RSS feed: missing channel element")
        items = channel.findall("item")
    elif root.tag == "{http://www.w3.org/2005/Atom}feed":
        feed_type = "atom"
        channel = root
        items = channel.findall(".//{http://www.w3.org/2005/Atom}entry")
    elif root.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF":
        feed_type = "rdf"
        channel = root
        items = channel.findall(".//{http://purl.org/rss/1.0/}item")
        if not items:
            items = channel.findall("item")
    else:
        raise ValueError(f"Unknown feed type: {root.tag}")

    feed = _parse_feed_info(channel, feed_type)

    entries = []
    feed["entries"] = entries
    for item in items:
        entry = _parse_feed_entry(item, feed_type)
        categories = _collect_categories(item, feed_type)
        if categories:
            entry["category"] = categories[0]
            entry["tags"] = [{"term": value} for value in categories]
        entry["title"] = entry.get("title", "").strip()
        entry["description"] = entry.get("description", "").strip()
        entries.append(entry)

    return feed
