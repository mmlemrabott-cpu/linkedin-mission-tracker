---

name: linkedin-scraper

description: Scrapes LinkedIn posts mentioning freelance missions using the BeReach API.
  Invoked at the start of each daily run. Returns a list of raw post dicts.

---



\## Primary Scraper — BeReach API (`scraper/bereach_scraper.py`)

The active scraper uses the BeReach API (`https://api.berea.ch/search/linkedin/posts`).

1\. Authenticate using `BEREACH_API_TOKEN` environment variable.

2\. Build keyword queries dynamically from `config.search_keywords` × `config.target_countries`:

&nbsp;  - Each pair becomes a BeReach boolean query: `"<keyword>" AND (<country_term>)`

&nbsp;  - Country aliases: "France" → `France`, "Morocco" → `Maroc OR Morocco`

3\. Run all queries **sequentially** with a random delay of **12–18s between each query** to respect BeReach rate limits. Parallel execution caused retry backoffs to collide with new requests, triggering cascading HTTP 429 errors.

4\. Each query paginates (`start` offset) while `hasMore=True` and collected items < `max_posts_per_country`.

5\. Merge results across all batches and deduplicate by URL and text hash.

6\. Apply 24h safety filter: discard posts with `post_date < utcnow() - 24h`.

7\. Save raw results to `data/raw_posts_{YYYY-MM-DD}.json` before returning.

8\. Return: `List[RawPost]` — deduplicated, within 24h, cross-run duplicates removed.



\## Cross-Run Deduplication

Both scrapers accept `seen_urls` and `seen_hashes` sets (pre-loaded from the Dedup_Index sheet tab
by `sheets_writer.load_seen_posts_all_tabs()`). Posts matching these sets are discarded before
returning, avoiding redundant Claude scoring on already-stored posts.



\## RawPost Schema

Each post normalized to: `post_url`, `author_name`, `author_title`, `author_profile_url`,
`post_text`, `post_date` (UTC ISO string), `likes_count`, `comments_count`, `contact_info`,
`country`, `keyword`.



\## Environment Variables Required

\- `BEREACH_API_TOKEN`: BeReach API token (post scraping + profile fetching)

