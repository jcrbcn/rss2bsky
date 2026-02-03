## RSS to Bluesky - in Python

This is a proof-of-concept implementation for posting RSS/Atom content to Bluesky. Some hacking may be required. Issues and pull requests welcome to improve the system.

## Built with:

- [arrow](https://arrow.readthedocs.io/) - Time handling for humans
- [atproto](https://github.com/MarshalX/atproto) - AT protocol implementation for Python. The API of the library is still unstable, but the version is pinned in requirements.txt
- [fastfeedparser](https://github.com/kagisearch/fastfeedparser) - For feed parsing with a unified API
- [httpx](https://www.python-httpx.org/) - For grabbing remote media

## Features:

- Deduplication: The script queries the target timeline and only posts RSS items that are more recent than the latest top-level post by the handle.
- Filters: Easy to extend code to support filters on RSS contents for simple transformations and limiting cross-posts.
- Minimal rich-text support (links and tags): URLs on their own line are linked and inline hashtags are tagged.
- Link cards: Fetches basic metadata (title/description/image) for external embeds when available.
- Image references: Can forward preview images for link cards to Bsky.

## Usage and configuration

1. Start by installing the required libraries `pip install -r requirements.txt`
2. Run the script with command-line arguments:

`python rss2bsky.py <rss_feed_url> <bsky_handle> <bsky_username> <bsky_app_password>`

Arguments:

- `rss_feed_url`: RSS/Atom feed URL
- `bsky_handle`: Handle like `name.bsky.social`
- `bsky_username`: Email address associated with the account
- `bsky_app_password`: App password for the account
- `--translate-target`: Optional target language for DeepL (e.g. `ca`); requires `DEEPL_AUTH_KEY` in the environment
- `--category-format-file`: Optional JSON file mapping category to template (e.g. `{"F.C. Barcelona": "🔵🔴 {title} | #FCBarcelona"}`)

## GitHub Actions pipeline

This repo includes a scheduled GitHub Actions workflow at `.github/workflows/rss2bsky.yml`.
Create your own copy and structure to the desired RSS feeds you want to post.

How it runs:

- Schedule: every 30 minutes via cron
- Manual: supports `workflow_dispatch` from the GitHub UI
- Command: `python3 rss2bsky.py https://www.mundodeportivo.com/feed/rss/home ${{ secrets.BSKY_HANDLE }} ${{ secrets.BSKY_USERNAME }} ${{ secrets.BSKY_APP_PASSWORD }}`

Setup steps:

1. In your GitHub repo settings, add these secrets:
   - `BSKY_HANDLE`
   - `BSKY_USERNAME`
   - `BSKY_APP_PASSWORD`
   - `DEEPL_AUTH_KEY` (only required if you use `--translate-target`)
2. Optionally adjust the RSS feed URL and cron settings in `.github/workflows/rss2bsky.yml`.
3. Add extra pipelines to post from different RSS feeds in the same account

## DEEPL API Integration

To run translations, you need a free account on https://www.deepl.com/
