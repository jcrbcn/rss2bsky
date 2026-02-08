from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import arrow
import fastfeedparser_ext as fastfeedparser
import httpx
from atproto import Client, client_utils, models
from bs4 import BeautifulSoup

# From environment: when "true", skip posting and image uploads (dry run).
DEBUG_MODE = os.environ.get("DEBUG_MODE", "false").lower() == "true"

# Constants for trim_link_description
PHRASE_START_LIMIT = 150
TOTAL_DESC_LIMIT = 200

# Login retry backoff (seconds)
LOGIN_BACKOFF_INITIAL = 60
LOGIN_BACKOFF_MAX = 600

# HTTP timeouts (seconds)
HTTP_TIMEOUT = 10

# --- Logging (stderr so it appears in GitHub Actions / console) ---
logging.basicConfig(
    format="%(asctime)s %(message)s",
    stream=sys.stderr,
    level=logging.INFO,
)


@dataclass
class PendingPost:
    """One feed item prepared for posting: text, link, rich text, and optional translation."""

    rss_time: arrow.Arrow
    item: Any  # feed entry from fastfeedparser
    post_text: str
    title_text: str
    translated_title: Optional[str]
    link_for_post: str
    rich_text: Any  # TextBuilder from make_rich()


def fetch_link_metadata(url: str) -> dict[str, Any]:
    """Fetch Open Graph / meta title, description, and image from a URL. Returns a dict with keys title, description, image (or empty dict on error)."""
    try:
        r = httpx.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except httpx.HTTPError as e:
        logging.warning("Could not fetch link metadata for %s: %s", url, e)
        return {}
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.find("meta", property="og:title") or soup.find("title")
        desc = soup.find("meta", property="og:description") or soup.find(
            "meta", attrs={"name": "description"}
        )
        image = soup.find("meta", property="og:image") or soup.find(
            "meta", attrs={"name": "twitter:image"}
        )
        return {
            "title": (
                title["content"]
                if title and title.has_attr("content")
                else (title.text if title else "")
            ),
            "description": desc["content"] if desc and desc.has_attr("content") else "",
            "image": image["content"] if image and image.has_attr("content") else None,
        }
    except Exception as e:
        logging.warning("Could not parse link metadata for %s: %s", url, e)
        return {}


def get_last_bsky(client: Client, handle: str) -> arrow.Arrow:
    """Return the creation time of the most recent top-level post by the given handle, or epoch if none."""
    timeline = client.get_author_feed(handle)
    for titem in timeline.feed:
        # Only care about top-level, non-reply posts
        if titem.reason is None and getattr(titem.post.record, "reply", None) is None:
            logging.info("Record created %s", str(titem.post.record.created_at))
            return arrow.get(titem.post.record.created_at)
    return arrow.get(0)


def read_last_feed_check(state_file: Optional[str]) -> Optional[arrow.Arrow]:
    """Read last feed-check time from state file. Returns None if no file or invalid."""
    if not state_file:
        return None
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            line = f.read().strip()
            if line:
                return arrow.get(line)
    except (OSError, arrow.parser.ParserError) as e:
        logging.debug("Could not read state file %s: %s", state_file, e)
    return None


def write_last_feed_check(state_file: Optional[str], when: arrow.Arrow) -> None:
    """Write feed-check time to state file (ISO format)."""
    if not state_file:
        return
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(when.isoformat())
    except OSError as e:
        logging.warning("Could not write state file %s: %s", state_file, e)


def make_rich(content: str):
    """Build atproto TextBuilder with links and hashtags from plain text. Returns a TextBuilder instance."""
    url_pattern = re.compile(r"https?://[^\s]+")
    hashtag_pattern = re.compile(r"(#[^\W_]+)", flags=re.UNICODE)
    text_builder = client_utils.TextBuilder()
    lines = content.split("\n")
    for line in lines:
        # Identify URLs anywhere in the line and hyperlink them.
        cursor = 0
        for match in url_pattern.finditer(line):
            before = line[cursor : match.start()]
            if before:
                tag_split = hashtag_pattern.split(before)
                for t in tag_split:
                    if t.startswith("#"):
                        text_builder.tag(t, t[1:].strip())
                    else:
                        text_builder.text(t)
            url = match.group(0)
            text_builder.link(url, url)
            cursor = match.end()

        tail = line[cursor:]
        if tail:
            tag_split = hashtag_pattern.split(tail)
            for t in tag_split:
                if t.startswith("#"):
                    text_builder.tag(t, t[1:].strip())
                else:
                    text_builder.text(t)

        text_builder.text("\n")
    return text_builder


def get_image_blob(image_url: str, client: Client) -> Optional[Any]:
    """Download image from URL and upload as Bluesky blob; returns blob or None on failure or in DEBUG_MODE."""
    if DEBUG_MODE:
        return None
    try:
        r = httpx.get(image_url, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        return client.upload_blob(r.content).blob
    except Exception as e:
        logging.warning("Could not fetch/upload image from %s: %s", image_url, e)
        return None


def is_html(text: str) -> bool:
    """Return True if the string contains HTML-like tags."""
    return bool(re.search(r"<.*?>", text))


def trim_link_description(
    text: Optional[str],
    phrase_start_limit: int = PHRASE_START_LIMIT,
    total_limit: int = TOTAL_DESC_LIMIT,
) -> Optional[str]:
    """Trim description to whole phrases: include phrases starting within phrase_start_limit chars, then trim to total_limit. Returns None or empty if input is falsy."""
    if not text:
        return text
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return normalized

    phrases = []
    for match in re.finditer(r"[^.!?]+[.!?]?", normalized):
        phrase = match.group(0).strip()
        if phrase:
            phrases.append((match.start(), phrase))

    if not phrases:
        return normalized[:phrase_start_limit].rstrip()

    selected = []
    for idx, (start, phrase) in enumerate(phrases):
        if start <= phrase_start_limit:
            selected.append(phrase)

    if not selected:
        return normalized[:phrase_start_limit].rstrip()

    combined = " ".join(selected).strip()
    while len(selected) > 1 and len(combined) > total_limit:
        selected.pop()
        combined = " ".join(selected).strip()
    return combined


def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """Translate text via DeepL API. Requires DEEPL_AUTH_KEY. Returns original text if no target_lang or no text."""
    if not text or not target_lang:
        return text
    auth_key = os.environ.get("DEEPL_AUTH_KEY")
    if not auth_key:
        raise ValueError("DEEPL_AUTH_KEY is required for translation.")
    target_lang = target_lang.replace("_", "-").upper()
    res = httpx.post(
        "https://api-free.deepl.com/v2/translate",
        data={"text": text, "source_lang": source_lang, "target_lang": target_lang},
        headers={"Authorization": f"DeepL-Auth-Key {auth_key}"},
        timeout=HTTP_TIMEOUT,
    )
    res.raise_for_status()
    data = res.json()
    translations = data.get("translations") or []
    translated = translations[0].get("text") if translations else None
    if not translated:
        raise ValueError("DeepL returned no translated text.")
    return translated


def build_google_translate_url(url: str, target_lang: str) -> str:
    """Build a Google Translate URL that shows the given URL translated to target_lang. Returns original url if no target_lang."""
    if not url or not target_lang:
        return url
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    translated_host = f"{parsed.netloc.replace('.', '-')}.translate.goog"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "_x_tr_sl": "auto",
            "_x_tr_tl": target_lang,
            "_x_tr_hl": target_lang,
            "_x_tr_pto": "wapp",
        }
    )
    return urlunparse(
        (
            "https",
            translated_host,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


def load_category_format_file(file_path: Optional[str]) -> dict[str, str]:
    """Load JSON file mapping category name to format string (supports {title} and {category}). Returns empty dict if no path or on read/parse error."""
    if not file_path:
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {category.strip(): template for category, template in data.items()}
    except (OSError, json.JSONDecodeError) as e:
        logging.warning("Could not load category format file %s: %s", file_path, e)
        return {}


def format_post_text(
    title_text: str, category: str, category_formats: dict[str, str]
) -> str:
    """Format post text using category_formats template for category if present; otherwise return title_text. Templates support {title} and {category}."""
    matched_category = None
    template = None
    if category in category_formats:
        matched_category = category
        template = category_formats[category]
    if template:
        return template.format_map(
            {"title": title_text, "category": matched_category or ""}
        )
    return title_text


def parse_args() -> argparse.Namespace:
    """Build and parse command-line arguments. Returns the parsed namespace."""
    parser = argparse.ArgumentParser(description="Post RSS to Bluesky.")
    parser.add_argument("rss_feed", help="RSS feed URL")
    parser.add_argument("bsky_handle", help="Bluesky handle")
    parser.add_argument("bsky_username", help="Bluesky username")
    parser.add_argument("bsky_app_password", help="Bluesky app password")
    parser.add_argument(
        "--path-only",
        action="append",
        default=[],
        help=(
            "Only post items whose URL path matches this subpath (repeatable, "
            "e.g. --path-only futbol --path-only basket)"
        ),
    )
    parser.add_argument(
        "--translate-source",
        default="auto",
        help='Translate post text from source language (e.g. "en"). Requires DEEPL_AUTH_KEY.',
    )
    parser.add_argument(
        "--translate-target",
        default=None,
        help='Translate post text to target language (e.g. "ca"). Requires DEEPL_AUTH_KEY.',
    )
    parser.add_argument(
        "--translation-pretext",
        default="Original: ",
        help='Pretext to add to the translated text (e.g. "Automatic translation - See original:").',
    )
    parser.add_argument(
        "--category-format-file",
        default=None,
        help=(
            "Path to a JSON file mapping category to template. "
            "Template supports {title} and {category}."
        ),
    )
    parser.add_argument(
        "--spread-seconds",
        type=int,
        default=0,
        help=(
            "Spread all posts across this many seconds. "
            "For example, 600 spreads posts over 10 minutes."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=(
            "Path to a file storing the last feed-check time (ISO timestamp). "
            "Used so the cutoff is 'last time we checked the feed', not just 'last post time'."
        ),
    )
    return parser.parse_args()


def create_client(username: str, password: str) -> Client:
    """Log in to Bluesky with retry and exponential backoff. Returns authenticated Client."""
    client = Client()
    backoff = LOGIN_BACKOFF_INITIAL
    while True:
        try:
            client.login(username, password)
            return client
        except Exception:
            logging.exception("Login exception")
            time.sleep(backoff)
            backoff = min(backoff + LOGIN_BACKOFF_INITIAL, LOGIN_BACKOFF_MAX)


def fetch_new_feed_items(
    feed_url: str,
    path_only: list[str],
    cutoff: arrow.Arrow,
    category_formats: dict[str, str],
    translate_source: str,
    translate_target: Optional[str],
    translation_pretext: str,
) -> list[PendingPost]:
    """Parse feed, filter by path and time (rss_time > cutoff), apply formatting and translation. Returns list of PendingPost sorted by rss_time."""
    feed = fastfeedparser.parse(feed_url)
    new_items: list[PendingPost] = []
    for item in feed.entries:
        rss_time = arrow.get(item.published)
        if path_only:
            item_path = urlparse(item.link).path.lstrip("/")
            if not any(
                item_path == prefix or item_path.startswith(f"{prefix}/")
                for prefix in path_only
            ):
                continue
        logging.info("--------------------------------")
        logging.info("RSS Time: %s", str(rss_time))
        logging.info("Item: %s", item.title)
        logging.info("Item link: %s", item.link)
        if is_html(item.title):
            title_text = BeautifulSoup(item.title, "html.parser").get_text().strip()
        else:
            title_text = item.title.strip()
        category = item.tags[0]["term"] if item.tags else ""
        post_text = format_post_text(title_text, category, category_formats)
        translated_title: Optional[str] = None
        translated_link: Optional[str] = None
        if translate_target:
            translated_title = translate_text(
                title_text, translate_source, translate_target
            )
            translated_post_text = format_post_text(
                translated_title, category, category_formats
            )
            post_text = f"{translated_post_text}\n\n{translation_pretext}{item.link}"
            translated_link = build_google_translate_url(item.link, translate_target)
        link_for_post = translated_link if translated_link else item.link
        rich_text = make_rich(post_text)
        if rss_time > cutoff:
            new_items.append(
                PendingPost(
                    rss_time=rss_time,
                    item=item,
                    post_text=post_text,
                    title_text=title_text,
                    translated_title=translated_title,
                    link_for_post=link_for_post,
                    rich_text=rich_text,
                )
            )
        else:
            logging.debug("Not sending %s", item.link)
    new_items.sort(key=lambda e: e.rss_time)
    return new_items


def build_embed(
    entry: PendingPost,
    client: Client,
    translate_target: Optional[str],
    translate_source: str,
) -> Optional[Any]:
    """Build external link card or image embed for the post. Returns None if no embed available."""
    link_metadata = fetch_link_metadata(entry.item.link)
    thumb_blob = None
    if link_metadata.get("image"):
        thumb_blob = get_image_blob(link_metadata["image"], client)
    external_embed = None
    translated_description = None
    if translate_target and link_metadata.get("description"):
        translated_description = translate_text(
            trim_link_description(link_metadata.get("description")),
            translate_source,
            translate_target,
        )
    if link_metadata.get("title") or link_metadata.get("description"):
        external_embed = models.AppBskyEmbedExternal.Main(
            external=models.AppBskyEmbedExternal.External(
                uri=entry.link_for_post,
                title=(
                    entry.translated_title
                    or link_metadata.get("title")
                    or entry.title_text
                    or entry.item.link
                ),
                description=(
                    translated_description
                    or link_metadata.get("description")
                    or ""
                ),
                thumb=thumb_blob,
            )
        )
    else:
        logging.info("No link metadata for %s; skipping card", entry.item.link)
    embed = external_embed
    if not embed and thumb_blob and not DEBUG_MODE:
        alt_text = (
            entry.translated_title
            or entry.title_text
            or link_metadata.get("title")
            or "Preview image"
        )
        embed = models.AppBskyEmbedImages.Main(
            images=[
                models.AppBskyEmbedImages.Image(
                    alt=alt_text,
                    image=thumb_blob,
                )
            ]
        )
    return embed


def run_posting_loop(
    items: list[PendingPost],
    client: Client,
    spread_seconds: float,
    translate_target: Optional[str],
    translate_source: str,
) -> None:
    """Post each item to Bluesky (unless DEBUG_MODE), with optional delay between posts. For image-only embeds, appends link to content."""
    total_items = len(items)
    logging.info("New items to post: %d", total_items)
    sleep_seconds = 0.0
    if spread_seconds > 0 and total_items > 1:
        sleep_seconds = spread_seconds / total_items
        logging.info(
            "Spreading posts over %d seconds (%0.2f sec between posts)",
            int(spread_seconds),
            sleep_seconds,
        )
    for idx, entry in enumerate(items):
        embed = build_embed(entry, client, translate_target, translate_source)
        # When we have image-only embed (no external card), include link in the text we send
        if embed is not None and getattr(embed, "images", None) is not None:
            rich_text = make_rich(entry.post_text + "\n" + entry.link_for_post)
        else:
            rich_text = entry.rich_text

        if not DEBUG_MODE:
            try:
                client.send_post(rich_text, embed=embed)
                logging.info("Sent post %s", entry.item.link)
            except Exception:
                logging.exception("Failed to post %s", entry.item.link)
                continue
        else:
            logging.info("DEBUG_MODE: skipping post %s", entry.item.link)

        has_more = idx < total_items - 1
        if sleep_seconds > 0 and has_more:
            logging.info("Sleeping %0.2f seconds before next post", sleep_seconds)
            time.sleep(sleep_seconds)


def main() -> None:
    """Entry point: parse args, login, fetch new items, run posting loop."""
    args = parse_args()
    feed_url = args.rss_feed
    bsky_handle = args.bsky_handle
    bsky_username = args.bsky_username
    bsky_password = args.bsky_app_password
    path_only = args.path_only
    translate_target = args.translate_target
    translate_source = args.translate_source
    translation_pretext = args.translation_pretext
    category_formats = load_category_format_file(args.category_format_file)
    spread_seconds = max(0, args.spread_seconds)

    if DEBUG_MODE:
        logging.info("Optional parameters:")
        logging.info("Path Only: %s", path_only)
        logging.info("Translate Target: %s", translate_target)
        logging.info("Translate Source: %s", translate_source)
        logging.info("Translation Pretext: %s", translation_pretext)

    client = create_client(bsky_username, bsky_password)
    last_bsky = get_last_bsky(client, bsky_handle)
    logging.info("Last Bluesky post: %s", str(last_bsky))
    last_feed_check = read_last_feed_check(args.state_file)
    cutoff = max(last_bsky, last_feed_check) if last_feed_check else last_bsky
    if last_feed_check:
        logging.info("Last feed check: %s; cutoff: %s", last_feed_check, cutoff)
    new_items = fetch_new_feed_items(
        feed_url,
        path_only,
        cutoff,
        category_formats,
        translate_source,
        translate_target,
        translation_pretext,
    )
    run_posting_loop(
        new_items, client, spread_seconds, translate_target, translate_source
    )
    write_last_feed_check(args.state_file, arrow.utcnow())


if __name__ == "__main__":
    main()
