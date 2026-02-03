import argparse
import arrow
import fastfeedparser_ext as fastfeedparser
import json
import logging
import os
import re
import httpx
import time
from atproto import Client, client_utils, models
from bs4 import BeautifulSoup
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# --- Logging ---
LOG_PATH = "rss2bsky.log"
logging.basicConfig(
    format="%(asctime)s %(message)s",
    filename=LOG_PATH,
    encoding="utf-8",
    level=logging.INFO,
)


def fetch_link_metadata(url):
    try:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
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
        logging.warning(f"Could not fetch link metadata for {url}: {e}")
        return {}


def get_last_bsky(client, handle):
    timeline = client.get_author_feed(handle)
    for titem in timeline.feed:
        # Only care about top-level, non-reply posts
        if titem.reason is None and getattr(titem.post.record, "reply", None) is None:
            logging.info("Record created %s", str(titem.post.record.created_at))
            return arrow.get(titem.post.record.created_at)
    return arrow.get(0)


def make_rich(content):
    url_pattern = re.compile(r"https?://[^\s]+")
    text_builder = client_utils.TextBuilder()
    lines = content.split("\n")
    for line in lines:
        # Identify URLs anywhere in the line and hyperlink them.
        cursor = 0
        for match in url_pattern.finditer(line):
            before = line[cursor : match.start()]
            if before:
                tag_split = re.split("(#[a-zA-Z0-9]+)", before)
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
            tag_split = re.split("(#[a-zA-Z0-9]+)", tail)
            for t in tag_split:
                if t.startswith("#"):
                    text_builder.tag(t, t[1:].strip())
                else:
                    text_builder.text(t)

        text_builder.text("\n")
    return text_builder


def get_image_blob(image_url, client):
    try:
        r = httpx.get(image_url)
        if r.status_code != 200:
            return None
        return client.upload_blob(r.content).blob
    except Exception as e:
        logging.warning(f"Could not fetch/upload image from {image_url}: {e}")
        return None


def is_html(text):
    return bool(re.search(r"<.*?>", text))


def translate_text(text, target_lang):
    if not text or not target_lang:
        return text
    auth_key = os.environ.get("DEEPL_AUTH_KEY")
    if not auth_key:
        raise ValueError("DEEPL_AUTH_KEY is required for translation.")
    target_lang = target_lang.replace("_", "-").upper()
    res = httpx.post(
        "https://api-free.deepl.com/v2/translate",
        data={"text": text, "target_lang": target_lang},
        headers={"Authorization": f"DeepL-Auth-Key {auth_key}"},
        timeout=10,
    )
    res.raise_for_status()
    data = res.json()
    translations = data.get("translations") or []
    translated = translations[0].get("text") if translations else None
    if not translated:
        raise ValueError("DeepL returned no translated text.")
    return translated


def build_google_translate_url(url, target_lang):
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


def load_category_format_file(file_path):
    if not file_path:
        return {}
    with open(file_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return {category.strip(): template for category, template in data.items()}


def format_post_text(title_text, category, category_formats):
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


def main():
    # --- Parse command-line arguments ---
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
    args = parser.parse_args()
    feed_url = args.rss_feed
    bsky_handle = args.bsky_handle
    bsky_username = args.bsky_username
    bsky_password = args.bsky_app_password
    path_only = args.path_only
    translate_target = args.translate_target
    translation_pretext = args.translation_pretext
    category_formats = load_category_format_file(args.category_format_file)

    # --- Login ---
    client = Client()
    backoff = 60
    while True:
        try:
            client.login(bsky_username, bsky_password)
            break
        except Exception as e:
            logging.exception("Login exception")
            time.sleep(backoff)
            backoff = min(backoff + 60, 600)

    # --- Get last Bluesky post time ---
    last_bsky = get_last_bsky(client, bsky_handle)

    # --- Parse feed ---
    feed = fastfeedparser.parse(feed_url)

    for item in feed.entries:
        rss_time = arrow.get(item.published)
        logging.info("RSS Time: %s", str(rss_time))
        if path_only:
            item_path = urlparse(item.link).path.lstrip("/")
            if not any(
                item_path == prefix or item_path.startswith(f"{prefix}/")
                for prefix in path_only
            ):
                logging.debug("Skipping %s due to path filter %s", item.link, path_only)
                continue
        # Use only the plain title as content, and add the link on a new line
        if is_html(item.title):
            title_text = BeautifulSoup(item.title, "html.parser").get_text().strip()
        else:
            title_text = item.title.strip()
        if item.tags:
            category = item.tags[0]["term"]
            logging.info("Category: %s", category)
        else:
            category = ""
        post_text = format_post_text(title_text, category, category_formats)
        translated_title = None
        translated_link = None
        if translate_target:
            translated_title = translate_text(title_text, translate_target)
            translated_post_text = format_post_text(
                translated_title, category, category_formats
            )
            post_text = f"{translated_post_text}\n\n{translation_pretext}{item.link}"
            translated_link = build_google_translate_url(item.link, translate_target)
        link_for_post = translated_link if translated_link else item.link
        logging.info("Title+link used as content: %s", post_text)
        logging.info("Extracted category: %s", category)
        rich_text = make_rich(post_text)
        logging.info("Rich text length: %d" % (len(rich_text.build_text())))
        logging.info("Filtered Content length: %d" % (len(post_text)))
        if rss_time > last_bsky:  # Only post if newer than last Bluesky post
            # if True:  # FOR TESTING ONLY!
            link_metadata = fetch_link_metadata(item.link)
            thumb_blob = None
            if link_metadata.get("image"):
                thumb_blob = get_image_blob(link_metadata["image"], client)

            external_embed = None
            translated_description = None
            if translate_target and link_metadata.get("description"):
                translated_description = translate_text(
                    link_metadata.get("description"), translate_target
                )
            if link_metadata.get("title") or link_metadata.get("description"):
                logging.info("Using link card for %s", item.link)
                external_embed = models.AppBskyEmbedExternal.Main(
                    external=models.AppBskyEmbedExternal.External(
                        uri=link_for_post,
                        title=translated_title
                        or link_metadata.get("title")
                        or title_text
                        or item.link,
                        description=translated_description
                        or link_metadata.get("description")
                        or "",
                        thumb=thumb_blob,
                    )
                )
            else:
                logging.info("No link metadata for %s; skipping card", item.link)
            embed = external_embed
            if not embed and thumb_blob:
                post_text = f"{post_text}\n{link_for_post}"
                alt_text = (
                    translated_title
                    or title_text
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

            # Post
            try:
                client.send_post(rich_text, embed=embed)
                logging.info("Sent post %s" % (item.link))
            except Exception as e:
                logging.exception("Failed to post %s" % (item.link))
        else:
            logging.debug("Not sending %s" % (item.link))


if __name__ == "__main__":
    main()
