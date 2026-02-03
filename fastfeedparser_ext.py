from __future__ import annotations

import gzip
import zlib
from urllib.request import (
    HTTPErrorProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from lxml import etree

from fastfeedparser.main import FastFeedParserDict, _parse_feed_entry, _parse_feed_info


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


def parse(source: str | bytes) -> FastFeedParserDict:
    """Parse a feed from a URL or XML content, with category extraction."""
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        request = Request(
            source,
            method="GET",
            headers={
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": (
                    "fastfeedparser (+https://github.com/kagisearch/fastfeedparser)"
                ),
            },
        )
        opener = build_opener(HTTPRedirectHandler(), HTTPErrorProcessor())
        with opener.open(request, timeout=30) as response:
            response.begin()
            content: bytes = response.read()
            content_encoding = response.headers.get("Content-Encoding")
            if content_encoding == "gzip":
                content = gzip.decompress(content)
            elif content_encoding == "deflate":
                content = zlib.decompress(content, -zlib.MAX_WBITS)
            content_charset = response.headers.get_content_charset()
            xml_content = (
                content.decode(content_charset) if content_charset else content
            )
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
        raise ValueError(f"Failed to parse XML content: {str(e)}")
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
