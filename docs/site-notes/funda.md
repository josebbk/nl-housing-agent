# Funda — Site Notes

Running log of scraper breakage on Funda: what changed, how it was diagnosed,
and how it was fixed. Read this file before debugging any Funda scraper issue
— the fix may already be documented, or the underlying pattern may already
be known.

## Known Limitations

### parking_type (and garage_type) combined "TypeA + TypeB" stored values

parking_type (and separately, garage_type) can be stored as a combined
"TypeA + TypeB" string (e.g. "Op eigen terrein + Parkeergarage"). This
happens when the extraction layer joins two detected values.

Scoring currently works around this by splitting on "+" and using only
the first segment for classification. This means a combined value like
"Op eigen terrein + Parkeergarage" scores as "Op eigen terrein" (0.9)
rather than potentially using the better of the two values (1.0 for
Parkeergarage).

Proper multi-value handling — splitting into a list at extraction time
and scoring the best of multiple values — is deferred to a future task
that addresses the root cause in the extraction layer.

## Entries

(newest entries at the top)

### 2026-08-19 — Zero-result total count still scraped all max_pages

- **Symptom:** When the extracted total listing count was genuinely 0
  (confirmed via log "Total listing count is 0 — scraping 0 pages."),
  the scraper still fetched and processed all 5 pages instead of stopping
  after page 1.

- **Diagnosis:** In `scrape_funda()` the `else` branch (triggered when
  `total_count == 0` or extraction failed) unconditionally set
  `pages_to_scrape = max_pages` *before* checking whether `total_count == 0`.
  The log line correctly said "scraping 0 pages" but the variable was
  already 5, so the `for page_num in range(1, pages_to_scrape + 1)` loop
  still fetched all pages. The fix separates the two cases: `total_count == 0`
  sets `pages_to_scrape = 0`, while extraction failure (None) falls back to
  `max_pages` as before.

- **Fix:** Moved `pages_to_scrape = 0` into the `if total_count == 0`
  branch and `pages_to_scrape = max_pages` into the `else` (extraction
  failure) branch, so the two cases are no longer conflated.

- **Pattern/Warning:** When a computed value can be 0 and 0 is a valid
  "do nothing" result, never use a truthy/falsy check (`if computed_pages:`)
  to distinguish it from "could not compute" (None). Always check explicitly
  for `is None` or `== 0` separately.

### 2026-08-19 — Dynamic page-count detection via "N koopwoningen" text

- **Symptom:** The scraper always scraped 5 pages (max_pages default)
  regardless of how many actual listings matched the current filter
  criteria. For narrow filters returning fewer than 75 listings this
  wasted requests on empty or near-empty pages 4-5.

- **Diagnosis:** Funda's search results page displays the total matching
  listing count near the top of the page as plain text in the format
  "N koopwoningen". Confirmed real examples:
  * Normal:  "218 koopwoningen"
  * Low:     "1 koopwoningen"
  * Zero:    "0 koopwoningen binnen jouw zoekwensen" (different trailing
    text from the normal case)
  Each results page contains 15 unique listings (confirmed in prior
  entries). Formula: pages = ceil(total_count / 15).

- **Fix:** Added `extract_total_listing_count(page_text)` in
  `scraper.py` that uses regex `(\d[\d.]*)\s+koopwoningen` to extract
  the count from `document.body.innerText`. Dutch thousands-separator
  dots are stripped (same convention as `parse_price()`). If extraction
  fails or returns None, the scraper falls back to the caller-provided
  `max_pages` unchanged. `scrape_funda()` now does a preliminary fetch
  of page 1, extracts the count, computes `computed_pages = ceil(total / 15)`,
  and scrapes `min(max_pages, computed_pages)` pages total.

- **Pattern/Warning:** The "N koopwoningen" text is part of Funda's
  rendered page body text (not a CSS selector), so it is stable across
  DOM changes. However, if Funda changes the Dutch wording (e.g. to
  "resultaten" or English), the regex will silently fail and fall back
  to max_pages. Monitor for sudden increases in page requests per run.

### 2026-08-18 — Detail-page fields erased by card-only re-inserts (storage.py)

- **Symptom:** On the first scraper run, a listing's Phase-2 detail-page
  fields (ownership_type, garden_present, insulation_score, parking_type,
  bathrooms, rooms, year_built, etc.) were correctly populated in the
  database after a detail-page fetch. On the SECOND run, when the same
  listing was re-encountered via the normal card/results-page scrape and
  the detail page was NOT re-fetched, the previously stored detail-page
  field values were being overwritten with NULL in the database.

- **Diagnosis:** `storage.py::insert_listing()` unconditionally defaulted
  every phase2_fields entry and optional fields (rooms, year_built) to
  None whenever the key was missing from the incoming dict. For an
  existing listing, the UPDATE branch built a set_clause from all
  updatable columns and wrote `data.get(col)` for each — which was
  always None for detail-only fields on a card-level re-insert.

  The card scraper (`scraper.py::_extract_listing_data`) returns a dict
  with `"rooms": None` and `"year_built": None` explicitly set, and
  never includes any of the 20 phase2_fields keys. The detail scraper
  (`detail_scraper.py::fetch_listing_details`) returns a dict via
  `DetailData.to_dict()` which filters out None values, so absent
  detail fields are indistinguishable from "not scraped" at the
  `insert_listing()` call boundary.

  The codebase already implemented the correct fix pattern for one field
  (`status`): preserve existing non-None value when new value is None.
  This was never generalized to the other detail-only fields.

- **Fix:** Generalized the existing `status` preservation pattern to all
  detail and shared fields in `storage.py::insert_listing()`. When an
  existing DB value is non-None and the incoming data is None, the
  existing value is preserved. Protected fields: rooms, year_built,
  plot_size_m2, property_type, energy_label, and all 20 phase2_fields.
  Card-level fields (url, address, neighborhood, price, living_area_m2,
  bedrooms) remain freely overwritable.

- **Pattern/Warning:** The `insert_listing()` function is the single
  convergence point for ALL listing data from ALL callers (card scraper,
  detail scraper, backfill, seed). Any field that is not present in the
  incoming dict will default to None and be written to the UPDATE query.
  Always distinguish between "this field is not part of this data source"
  and "this field has changed to None." The preservation pattern
  (existing non-None + new None → preserve existing) must be applied to
  all detail-only and shared optional fields, but NOT to card-level
  fields that must be freely overwritten.

### 2026-08-18 — ~28 listings lost due to two different Funda card DOM templates

- **Symptom:** The scraper returned 47 listings across 5 pages instead of
  the expected ~75. Diagnostic counting showed: every page had 30 total
  `<a href*="/detail/koop/">` links, 15 unique after href dedup, but
  6–9 were dropped at the "no card ancestor" stage on each page. Zero
  "no flexRow" or "short text" drops.

- **Diagnosis:** Funda uses **two different card DOM templates** on the same
  search results page:

  1. **Standard cards** (majority): `link → <div class="relative overflow-hidden ...">`
     → `<div class="flex flex-col @lg:flex-row">` (flexRow with listing data).
     This is the template the existing code handled correctly.

  2. **Promoted/featured cards** ("Blikvanger"): `link → <div class="">` (empty
     parent, no wrapper classes). The listing data lives directly in the
     link's parent `innerText`. These cards also have a different text format:
     the first line is a promotional description (e.g. "Ruim wonen aan een
     kindvriendelijk woonerf, met een zonnige tuin."), followed by concatenated
     badge words ("BlikvangerNieuw"), then the actual address.

  The existing code only handled template #1. Template #2 listings were
  silently dropped because the `relative overflow-hidden` ancestor walk
  failed. Across 5 pages, 28 listings were lost this way (6+6+1+9+6).

  Additionally, the address parser had two bugs with promoted card text:
  (a) The badge word "nieuw" was matching as a substring of "Nieuwe" in
      street names (e.g. "Nieuwe Osdorpergracht"), causing the real address
      to be skipped. (b) Promotional description lines were being accepted
      as addresses because they didn't match the existing skip heuristics.

- **Fix:**
  1. Added a fallback in `_extract_page_listings()`: when the
     `relative overflow-hidden` card walk fails, try `link.parentElement`
     directly. If the parent's `innerText` contains a price (€) and enough
     text, use it as the listing text source.
  2. Fixed the address parser in `_extract_listing_data()`:
     - Badge word detection now uses a regex that matches badge words as
       complete tokens (not substrings of longer words), using `^(badge1|
       badge2|...)+$` for concatenated badges like "BlikvangerNieuw".
     - Added skip for promo description lines: lines longer than 40 chars
       containing commas are treated as promotional text.
     - Added skip for postcode+city lines (e.g. "1068 HV Amsterdam").

- **Pattern/Warning:** Funda uses at least two different card DOM templates
  on search results pages. The `relative overflow-hidden` wrapper is not
  universal — always have a fallback for listings whose links don't have
  this ancestor. Promoted/"Blikvanger" cards have a distinct text format
  with a promo description line followed by concatenated badge words.
  When debugging listing loss, always check for multiple DOM templates.

### 2026-08-17 — Listing loss due to brittle per-page dedup key

- **Symptom:** The scraper was returning ~47 listings across 5 pages instead
  of the expected ~80. Listings were being silently dropped during
  `_extract_page_listings()` without any error or warning.

- **Diagnosis:** The JavaScript dedup key in `_extract_page_listings()` used
  `card.innerHTML.substring(0, 200)` to detect duplicate cards on a page.
  Funda's card template has identical CSS classes and structure for every
  card — the first 200 characters of innerHTML are nearly identical across
  different listings. Only the variable content (address, price, area)
  differs, and this often appears after position 200. This caused distinct
  listings to collide on the dedup key, with one silently dropped.

  Confirmed by simulating two cards with different addresses but identical
  CSS class structure in the first 200 characters — the dedup keys were
  identical.

  Additionally, `_extract_listing_data()` silently returned `None` when the
  listing_id regex failed to match, with no logging to indicate a listing
  was dropped.

- **Fix:**
  1. Changed the JavaScript dedup key from `card.innerHTML.substring(0, 200)`
     to the href itself. Each unique listing has exactly one href
     (e.g., `/detail/koop/amsterdam/huis-x/12345/`), so this is guaranteed
     unique. Multiple links on the same card (image + text) share the same
     href, so this correctly skips them.
  2. Moved the dedup check to before the DOM traversal (more efficient —
     skip processing entirely if we've seen this href).
  3. Added `logger.warning()` when `_extract_listing_data()` drops a listing
     due to regex failure, so future drops are visible in logs.

- **Pattern/Warning:** Never use HTML content as a dedup key for listings.
  Always use a guaranteed-unique identifier (listing_id or href). HTML
  content can collide across distinct items, especially when the template
  structure is fixed and only variable data differs.

### 2026-08-16 — Amenities extraction removed from codebase

The amenities (`Voorzieningen`) extraction and scoring logic has been
removed from the codebase entirely. The entries below this one document
the historical debugging work that was done on the amenities extraction
logic. They are preserved as historical/reference only — they describe
site structure knowledge that is still true, but the extraction code
that depended on them no longer exists.

### 2026-08-16 — Amenities matched always empty in scoring

- **Symptom:** `amenities_raw` was being scraped correctly from the detail
  page, but `amenities_matched` in the scoring output was always an empty
  list. The `_score_amenities()` function in `scoring.py` returned a score
  of 0.0 for all listings because no keywords matched.

- **Diagnosis:** The root cause was in `detail_scraper.py`'s `fetch_listing_details()`.
  The "Voorzieningen" field value was being extracted from the wrong location
  within the "Indeling" subsection. The extraction regex was capturing text
  that did not start with an amenity keyword, resulting in raw text that
  couldn't match the tracked keyword dictionary in `preferences.json`. The fix
  (already in place from prior work) added amenity-keyword filtering: only
  matches where the value after "Voorzieningen" starts with a known amenity
  keyword (airconditioning, glasvezelkabel, alarminstallatie, etc.) are
  accepted as the correct field value.

- **Fix:** The `amenities_raw` extraction in `fetch_listing_details()` now:
  1. Finds all "Voorzieningen" (capital V) not preceded by "Badkamer" in the
     Indeling subsection.
  2. Takes the last match where the value starts with a known amenity keyword.
  3. Extracts the value until the next field boundary or end of section.

- **Verification:** Confirmed by fetching all 4 reference URLs fresh:
  - Amsterdam (Hilversumstraat 60): matched 4/4 keywords
  - Mill (Mergen 20): matched 3/3 keywords
  - Wijchen (Zevendreef 3079): matched 2/2 keywords
  - Aalsmeer (Zeeltstraat 19): matched 3/3 keywords, "dakraam" correctly NOT matched

- **Pattern/Warning:** The "Voorzieningen" label appears multiple times on
  Funda detail pages with different meanings. Always scope extraction to the
  "Indeling" subsection and validate that the captured value starts with a
  known amenity keyword. The `_score_amenities()` function in `scoring.py`
  is correct — it does substring matching against `amenities_tracked` from
  `preferences.json`. The bug was entirely in the data extraction layer, not
  the scoring layer.

### 2026-08-15 — Detail page field extraction: no-separator format, subsection parsing, duplicate text

- **Symptom:** After fixing energy_label/Voorzieningen/garden_size bugs, many
  fields still returned wrong values or None: property_type, year_built, status,
  plot_size_m2, garden_type, garden_orientation, insulation_raw, heating_type,
  boiler_year, amenities_raw, garage_type, parking_type, building_bound_outdoor_m2,
  erfpacht_canon_annual.

- **Diagnosis:**
  1. **No-separator format:** Funda's rendered text has NO separator between
     field names and values (e.g., "Bouwjaar1969", "StatusBeschikbaar").
     The previous `_extract_field_value()` function looked for `:` or `-`
     separators which never matched. Fixed by adding `_extract_field_until_next()`
     that handles both newline-separated and concatenated formats.
  2. **Duplicate text:** Funda's rendered text contains duplicate content:
     a concatenated block followed by a newline-separated block with the same
     data. Extraction functions had to handle both formats, preferring the
     newline-separated format for reliability.
  3. **Section splitting:** The `_split_sections()` function splits by lines
     matching known section headings. But section headings in Funda's text
     often appear without spaces between them (e.g., "KenmerkenOverdracht...").
     Added `_split_concatenated_sections()` as fallback.
  4. **Subsection parsing:** Kenmerken contains subsections (Overdracht, Bouw,
     Energie, etc.) that are concatenated without separators. The
     `_split_kenmerken_subsections()` function finds subsection headings using
     regex, but field names like "Soort garage" contain "garage" as a substring,
     causing false matches. Fixed by deduplicating subsections (keeping only
     the first occurrence of each).
  5. **Word boundary issues:** Regex patterns using `(?<!\w)` and `(?!\w)`
     failed because subsection headings are preceded/followed by word chars
     in concatenated text (e.g., "achteromGarageSoort"). Removed word-boundary
     assertions from subsection parsing.
  6. **Re.escape escaping spaces/hyphens:** `re.escape("Warm water")` escaped
     the space, breaking pattern matching. Fixed by not using re.escape for
     next_fields in boundary detection.
  7. **Missing end-of-string boundary:** `_extract_field_until_next()` with
     next_fields failed when the value didn't end with a known field name
     (e.g., "Soort garageCarport" where "Carport" is not in next_fields).
     Added `$` as a fallback boundary.
  8. **"Buitenruimte" in "Gebouwgebonden buitenruimte":** The subsection
     regex matched "buitenruimte" inside "Gebouwgebonden buitenruimte",
     truncating the "Oppervlakten en inhoud" subsection body. Added a filter
     to skip "buitenruimte" matches preceded by "gebouwgebonden".
  9. **"Voorzieningen" false matches:** The Indeling subsection contains
     "Badkamervoorzieningen" (substring match) and free-text "voorzieningen"
     (lowercase in descriptions like "dagelijkse voorzieningen"). The actual
     field is "Voorzieningen" (capital V) not preceded by "Badkamer", and
     its value always starts with an amenity keyword. Added filtering to
     find the correct match.

- **Fix:**
  1. Added `_extract_field_until_next()` with three strategies: newline-separated
     format, concatenated format with boundary-aware capture, and simple
     newline termination. Takes first line only to handle duplicate content.
  2. Rewrote `_split_sections()` to handle both newline-separated and
     concatenated sections.
  3. Rewrote `_split_kenmerken_subsections()` with deduplication to handle
     substring matches (e.g., "garage" in "Soort garage").
  4. Added extractors for missing fields: property_type, year_built, status,
     plot_size_m2, garden_orientation (compass extraction), boiler_year (from
     Energie section's last Cv-ketel occurrence).
  5. Fixed insulation_raw to handle bodies that start directly with the value
     (no "Isolatie" prefix).
  6. Fixed garden_orientation to extract just the Dutch compass direction
     (zuiden, noordwesten, etc.) from the description.
  7. Added `$` fallback boundary to `_extract_field_until_next()` so
     non-greedy capture doesn't fail when no next_field is found.
  8. Added filter in `_split_kenmerken_subsections()` to skip "buitenruimte"
     when preceded by "gebouwgebonden".
  9. Added amenity-keyword filtering for Voorzieningen extraction to skip
     false matches in free text and "Badkamervoorzieningen".

- **Pattern/Warning:** Funda's detail page text has a dual format: a concatenated
  block (no separators) followed by a newline-separated block (duplicates).
  Always prefer the newline-separated format for field extraction. When that
  fails, fall back to the concatenated format with boundary-aware capture.
  Subsection headings may be substrings of field names — always deduplicate
  by keeping only the first occurrence of each heading. When extracting fields
  that share labels across subsections (e.g., "Voorzieningen"), use content
  heuristics (e.g., "value starts with amenity keyword") to distinguish the
  correct source.

### 2026-08-15 — Multiple field-location bugs: energy_label, Voorzieningen reuse, garden size dynamic labels, neighborhood price outside Kenmerken

- **Symptom:** `energy_label` returned garbage values like "114 m²wonen" instead of grade letters (A-G).
  `amenities_matched` was empty because Voorzieningen was being read from the wrong subsection.
  Garden size extraction failed when the size field label was dynamic (matched the garden type value).
  `neighborhood_avg_price_m2` was never found because it lives outside the Kenmerken container.

- **Diagnosis:**
  1. **energy_label:** The "Energielabel" field lives inside the **"Energie"** subsection
     of Kenmerken (siblings: Isolatie, Verwarming, Warm water, Cv-ketel). The previous code
     searched the entire page text and matched "energielabel" in the compact icon-stat row near
     the top (where it appears as "Cenergielabel" with no label prefix), capturing unrelated text.
     The Energie section uses "FieldnameValue" format with **no separator** (e.g.
     "EnergielabelCIsolatieDakisolatie..."), so `_extract_field_value()` (which looks for `:` or `-`)
     never found it. The fix uses a regex matching "Energielabel" followed immediately by a grade
     letter pattern `[A-G][+]*`.
  2. **Voorzieningen reuse confirmed third location:** "Voorzieningen" appears in THREE subsections:
     - **"Indeling"** (correct source: living amenities like airconditioning, TV kabel, etc.)
     - **"Garage"** (wrong: electricity/water hookups for garage)
     - **"Bergruimte"** (wrong: "Elektra" — shed utilities, different meaning entirely)
     The fix scopes positively to "must be inside Indeling" rather than hardcoding exclusions.
  3. **Garden size dynamic labels:** The "Tuin" field's value (e.g. "Achtertuin") becomes the
     label of the next field holding the size (e.g. "Achtertuin: 76 m² (8,00 meter diep en 9,54 meter breed)").
     The size field label is NOT a fixed string like "Tuingrootte". The fix extracts the garden
     type first, then uses it as a dynamic regex pattern to find the size.
  4. **neighborhood_avg_price_m2:** "Gem. vraagprijs / m²" is in the **"Buurt"** section, which is
     a top-level page section OUTSIDE the Kenmerken container entirely (a sibling to Kenmerken).
     If extraction is scoped to only search inside Kenmerken, it will never find this field.
     The code correctly reads from the "Buurt" section via `_find_section(sections, "Buurt")`.

- **Fix:**
  1. Added `energy_label` field to `DetailData`. Added `_extract_energy_label()` that uses
     `re.search(r"Energielabel\s*([A-G][+]*)", body)` to handle the no-separator format.
     Extracted from the "Energie" subsection via `_find_section(sections, "Energie")`.
  2. Voorzieningen extraction already scoped to "Indeling" subsection in prior uncommitted changes.
  3. Rewrote `_extract_garden()` to first extract the garden type, then use it as a dynamic
     regex pattern to find the size field. Falls back to generic "Tuin" and "grootte" patterns.
  4. Confirmed `neighborhood_avg_price_m2` extraction already correctly reads from "Buurt" section.

- **Pattern/Warning / Field Location Reference Table:**

  | Field | Kenmerken Subsection | Notes |
  |-------|---------------------|-------|
  | Energielabel | **Energie** | No colon separator; use regex `[A-G][+]*` after "Energielabel" |
  | Isolatie | Energie | Sibling of Energielabel |
  | Verwarming | Energie | Sibling of Energielabel |
  | Cv-ketel | Energie | Sibling of Energielabel |
  | Warm water | Energie | Sibling of Energielabel |
  | Bouwjaar | **Bouw** | Separate subsection |
  | Eigendomssituatie | **Kadastrale gegevens** | Separate subsection |
  | Voorzieningen | **Indeling** | Label REUSED in "Garage" and "Bergruimte" with different meanings — always scope to Indeling |
  | Aantal kamers | Indeling | Sibling of Voorzieningen |
  | Aantal badkamers | Kenmerken (top-level) | Not in a subsection |
  | Gem. vraagprijs / m² | **Buurt** | Outside Kenmerken entirely — top-level page section |

### 2026-08-15 — "Voorzieningen" label reused across multiple Kenmerken subsections

- **Symptom:** `amenities_raw` was being sourced from the free-text
  "Omschrijving" description block instead of a structured field.
  `amenities_matched` always came back empty because prose text doesn't
  cleanly match the keyword dictionary.

- **Diagnosis:** The label "Voorzieningen" appears **twice** on a Funda
  detail page within the Kenmerken section:
  1. Under **"Indeling"** (siblings: Aantal kamers, Aantal badkamers,
     Aantal woonlagen) — this is the correct source: living-space amenities
     like airconditioning, alarminstallatie, buitenzonwering, TV kabel, etc.
  2. Under **"Garage"** (siblings: Soort garage, Capaciteit) — this refers
     to garage electricity/water hookups, unrelated to the amenities we track.
  Additionally, "Voorzieningen" can appear in the "Omschrijving" free-text
  description block, which is prose and doesn't match the keyword dictionary.
  The previous extraction code used `_find_text_block(text, "Voorzieningen")`
  which searched the entire page text and could land in any of these locations.

- **Fix:** Changed `fetch_listing_details()` in `detail_scraper.py` to
  specifically target the "Indeling" subsection and extract "Voorzieningen"
  from within it only. Added "indeling" to the known section headings in
  both `_split_sections()` and `_find_text_block()`. The extraction now:
  1. Finds the "Indeling" section body.
  2. Extracts the "Voorzieningen" field value from within that body.
  Never reads from "Omschrijving" or the "Garage" subsection.

- **Pattern/Warning:** Funda reuses field labels across different subsections
  within the same parent section (Kenmerken). The same label can appear
  multiple times with different meanings depending on which subsection it's
  in. When extracting fields, always scope to the specific subsection
  container rather than searching the entire page text. This applies not just
  to "Voorzieningen" but potentially to any field label that might be reused.

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