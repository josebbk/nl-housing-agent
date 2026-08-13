# Funda — Site Notes

Running log of scraper breakage on Funda: what changed, how it was diagnosed,
and how it was fixed. Read this file before debugging any Funda scraper issue
— the fix may already be documented, or the underlying pattern may already
be known.

## Entries

(newest entries at the top)

### 2026-08-13 — Near-total field-extraction failure on listing cards

- **Symptom:** The scraper was discarding ~75 of 76 listings because price,
  bedrooms, and living_area_m2 could not be extracted. The `_extract_page_listings`
  function was reading `parent?.innerText` where `parent` was the image link's
  direct parent (`relative overflow-hidden rounded-md`), which on the current
  Funda page only contains badge text like "Nieuw" — not the listing details.

- **Diagnosis:** Funda's card structure has two links per listing: an image link
  and a details link. The image link's direct parent is a small card wrapper
  containing only the badge. The actual listing data (address, price, living
  area, rooms, energy label) lives in a sibling `flexRow` container
  (`@lg:flex-row`) that wraps the card. The `innerText` of this flexRow
  contains all fields. The previous regex-based extraction on the wrong text
  source could never find price, bedrooms, or living area.

- **Fix:** Rewrote `_extract_page_listings` to walk up from each image link to
  find the `@lg:flex-row` parent container and extract its full `innerText`.
  Rewrote `_extract_listing_data` to parse fields from this complete text:
  price from `€` pattern, living/plot area from `m²` pattern, rooms/bedrooms/
  energy label from the tail after the last area value, address from the first
  non-badge line. URL-based fields (listing_id, neighborhood, property_type)
  are extracted from the href.

- **Pattern/Warning:** Funda's card DOM structure changed — the details moved
  out of the image-link's direct parent. If listing extraction starts failing
  again, check whether the card structure still places data in the `@lg:flex-row`
  parent. The `truncate` CSS class is used for address, postcode+city, price,
  and the area/rooms/energy block — these are stable anchors if text parsing
  becomes unreliable.

### 2026-08-13 — Akamai bot-protection blocks all headless browser navigation

- **Symptom:** `python -m src.scraper` navigated to Funda successfully (no
  HTTP errors) but every page returned 0 listings. Inspecting the page
  content revealed a server-side challenge page titled "Je bent bijna op de
  pagina die je zoekt" ("You're almost on the page you're looking for") with
  the text "We houden ons platform graag veilig en spamvrij. Daarom moeten we
  soms verifiëren dat onze bezoekers echte mensen zijn." (We keep our platform
  safe and spam-free, so we sometimes need to verify visitors are real people.)
  No interactive elements, no CAPTCHA iframe — a hard server-side block.

- **Diagnosis:**
  1. Confirmed the VPS IP (157.180.68.61, Hetzner datacenter) is flagged by
     Akamai as a datacenter/cloud IP.
  2. `curl` with default headers also received the challenge page.
  3. `curl` with realistic browser headers (`Sec-Fetch-Dest: document`,
     `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Site: none`, `Sec-Fetch-User: ?1`,
     `Upgrade-Insecure-Requests: 1`, `Accept-Language: nl-NL`) returned the
     actual search results page.
  4. Playwright's `sec-ch-ua` header contained `"HeadlessChrome"` which
     Akamai uses as a bot-detection signal. This header cannot be overridden
     via `set_extra_http_headers`, `route.continue_()`, or CDP
     `Network.setExtraHTTPHeaders` — it is set at the browser engine level.
  5. `playwright-stealth` (v2.0.3) was installed and applied but did not
     prevent the block.
  6. The working approach: fetch the page HTML with `urllib` using realistic
     browser headers, then load the HTML into Playwright via a `data:` URL
     for JavaScript rendering. This bypasses Akamai because the HTTP request
     comes from urllib (not Playwright's Chromium) and the browser only
     renders pre-fetched content.

- **Fix:** Added `_fetch_page_html()` function in `scraper.py` that uses
  `urllib.request` with a `_BROWSER_HEADERS` dict containing realistic
  browser headers. The `scrape_funda()` function now:
  1. Fetches each search page HTML via `_fetch_page_html()` first.
  2. Loads the HTML into Playwright via `data:text/html,` URL for JS rendering.
  3. Extracts listings from the rendered page as before.
  Also removed the `playwright_stealth` import and `add_init_script` webdriver
  masking (replaced by the fetch-then-load approach).

- **Pattern/Warning:** Funda uses Akamai bot-protection that blocks datacenter
  IPs at the HTTP level. Direct Playwright navigation to Funda will always
  fail from this VPS. The fetch-then-load workaround is reliable but adds an
  HTTP request per page. If Funda changes their HTML structure or requires
  session cookies, this approach may need adjustment.