RSS to Bluesky - in Python
--------------------------

This is a proof-of-concept implementation for posting RSS/Atom content to Bluesky. Some hacking may be required. Issues and pull requests welcome to improve the system.

## Built with:

* [arrow](https://arrow.readthedocs.io/) - Time handling for humans
* [atproto](https://github.com/MarshalX/atproto) - AT protocol implementation for Python. The API of the library is still unstable, but the version is pinned in requirements.txt
* [fastfeedparser](https://github.com/kagisearch/fastfeedparser) - For feed parsing with a unified API
* [httpx](https://www.python-httpx.org/) - For grabbing remote media


## Features:

* Deduplication: The script queries the target timeline and only posts RSS items that are more recent than the latest top-level post by the handle.
* Filters: Easy to extend code to support filters on RSS contents for simple transformations and limiting cross-posts.
* Minimal rich-text support (links and tags): URLs on their own line are linked and inline hashtags are tagged.
* Link cards: Fetches basic metadata (title/description/image) for external embeds when available.
* Image references: Can forward preview images for link cards to Bsky.

## Usage and configuration

1. Start by installing the required libraries `pip install -r requirements.txt`
2. Copy the configuration file and then edit it `cp config.json.sample config.json`
3. Run the script like `python rss2bsky.py`

The configuration file accepts the configuration of:

* a feed URL
* bsky parameters for a handle, username, and password
  * Handle is like name.bsky.social
  * Username is the email address associated with the account.
  * Password is your password. If you have a literal quote it can be escaped with a backslash like `\"`
* sleep - the amount of time to sleep while running
