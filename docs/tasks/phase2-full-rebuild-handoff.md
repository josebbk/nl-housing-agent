# Phase 2 Scoring — Full Rebuild Handoff

## Context for whoever picks this up

The project's `main` branch was reset to commit `76cd1438` ("Move Phase 2
search filters from .env to config/filters.json"). Everything below was
built and verified in a prior session but never committed, and is now gone
from the codebase. This document specifies the correct FINAL state directly
(informed by everything already debugged and verified), rather than
replaying the trial-and-error that got there — implement it as a spec, not
a story.

**Current HEAD already has (confirmed via GitHub):** the original Phase 2
scaffolding — `detail_scraper.py`, `scoring.py`, the `ALTER TABLE` schema
additions in `storage.py`, score display in `notifier.py`, and the
co-worker's configurable filters (`src/config.py`'s `FilterConfig`,
`config/filters.json`). It does NOT have any of the fixes/changes below.

**Known immediate bug in this HEAD, explained by item 1 below:**
`insert_listing` throws `Error binding parameter 29: type 'list' is not
supported` — root cause confirmed: `scoring.py`'s `_score_amenities` sets
`detail["amenities_matched"]` to a raw Python list, which `storage.py`
binds directly to SQLite without JSON-serializing. This blocks every
detail-fetch/score from persisting. Fixed as a side effect of item 1
(amenities is being removed entirely, not patched).

---

## Design confirmation — WHEN detail-page fetching happens

This is not a change, just making it explicit since it matters for
correctness: detail-page fetching (and therefore scoring) must ONLY
happen for listings that are new/updated AND already pass the Phase 1
filters — i.e. the exact set of listings about to be notified, right
before notification. It is never triggered for every card result, and
it is never a separate/batched step run independently of the
notification flow. Card-page fields (price, address, bedrooms,
living_area_m2, etc.) get stored for every scraped listing immediately
(needed for dedup); detail-page fields (plot_size_m2, ownership_type,
garden data, score, etc.) only get filled in for a listing at the exact
moment it's about to be scored and notified. If the implementation in
this HEAD does this any other way (e.g. detail-fetching every card
result, or as a separate scheduled step), that's a bug — fix it to match
this description as part of item 6/7 below.

## Implementation order (dependencies matter — follow this sequence)

### 1. Remove amenities entirely

Remove from: `detail_scraper.py` (delete `Voorzieningen`/amenities
extraction), `scoring.py` (`_score_amenities` and its call site),
`config/preferences.json` (remove `"amenities"` from weights, remove
`"amenities_tracked"`), `storage.py` (drop `amenities_raw` and
`amenities_matched` columns — back up `data/funda.db` first, then
`ALTER TABLE ... DROP COLUMN`, verify row count unchanged before/after),
`notifier.py` (remove amenities line from notification breakdown).

Reason to record in `config/preferences-notes.md` (additive, keep old
rationale as history): the keyword-matching extraction proved unreliable
and required disproportionate debugging effort relative to scoring value,
so it was dropped.

### 2. Fix detail-page field extraction (the big one)

The original `detail_scraper.py` has systematic extraction bugs. Ground
truth below is hand-verified against 4 real Funda listings — use it to
test against, not just "does it run without error."

**Key structural facts about Funda detail pages** (source of most bugs):
- `Energielabel`, `Isolatie`, `Verwarming`, `Cv-ketel` all live under one
  `### Energie` subsection — NOT under `### Bouw`, and NOT the bare
  letter shown in the icon-stat row near the top of the page (that row
  has no reliable label attached, don't source from it).
- `Bouwjaar` is under `### Bouw`.
- `Eigendomssituatie` is under `### Kadastrale gegevens`, and **can
  repeat once per cadastral parcel** — if ANY parcel says "erfpacht" →
  ownership is erfpacht; else if any says "volle eigendom" → full.
- Garden size is NOT a fixed field name. `Tuin`'s value (e.g.
  "Achtertuin") names a SECOND field that holds the actual size — e.g. a
  field literally labeled `Achtertuin` holds "76 m² (8,00 meter diep en
  9,54 meter breed)". This second field does not always exist even when
  `Tuin` does.
- `Ligging tuin` (orientation) is a separate, also-not-always-present
  field — don't guess it from `Tuin`'s value.
- Three DIFFERENT fields can appear in `### Oppervlakten en inhoud` that
  are easy to confuse: `Overige inpandige ruimte` (indoor storage, NOT
  our target), `Gebouwgebonden buitenruimte` (building-attached outdoor
  space — THIS is what maps to `building_bound_outdoor_m2`), and
  `Externe bergruimte` (external storage, NOT our target). A listing can
  have two of these three present simultaneously with different values.
- `Soort garage` lives under `### Garage`, which is entirely absent when
  there's no garage (→ `None`, not a special value). Sometimes "Carport"
  appears here instead of a real garage.
- `Soort parkeergelegenheid` lives under `### Parkeergelegenheid`, can
  contain multiple space-separated values (e.g. "Betaald parkeren en
  openbaar parkeren") — classify by keyword priority: "eigen terrein"
  (best) > "carport" > "openbaar" > "betaald" (worst); pick the single
  best keyword actually present, don't return a combined string.
- `Gem. vraagprijs / m²` (→ `neighborhood_avg_price_m2`) is in a
  **top-level `## Buurt` section, OUTSIDE the Kenmerken container
  entirely** — sibling to it, not nested inside. Confirmed genuinely
  absent on 2 of 4 reference listings (smaller/rural areas) — must be
  `None`-safe, not an extraction failure.
- Page text comes in two different raw formats depending on rendering
  (concatenated vs. newline-separated) — the parser must handle both, not
  just one.

**Ground truth — 4 reference listings, use for verification:**

```
LISTING 1: https://www.funda.nl/detail/koop/amsterdam/huis-hilversumstraat-60/44480057/
rooms: 4 | property_type: "Eengezinswoning, hoekwoning" | year_built: 1969
energy_label: "C" | status: "Beschikbaar" | plot_size_m2: 163
ownership_type: "erfpacht" | erfpacht_canon_annual: 408.85
garden_present: true | garden_type: "Achtertuin" | garden_size_m2: 76
garden_orientation: "zuiden" | balcony_present: null
building_bound_outdoor_m2: null (has "Overige inpandige ruimte 20 m²" instead — do not map)
garage_type: "attached" | parking_type: "public" (paid+public present, public is best of the two)
insulation_raw: "Dakisolatie, gedeeltelijk dubbel glas en muurisolatie"
heating_type: "gas_boiler" | boiler_year: 2011 | bathrooms: 1
neighborhood_avg_price_m2: 5216

LISTING 2: https://www.funda.nl/detail/koop/mill/huis-mergen-20/80918937/
rooms: 7 | property_type: "Eengezinswoning, 2-onder-1-kapwoning" | year_built: 1993
energy_label: "A" | status: "Beschikbaar" | plot_size_m2: 244
ownership_type: "full" | erfpacht_canon_annual: null
garden_present: true | garden_type: "Achtertuin en voortuin" | garden_size_m2: 76 (field labeled "Achtertuin")
garden_orientation: "noordwesten" | balcony_present: null
building_bound_outdoor_m2: 20 (also has separate "Overige inpandige ruimte 23 m²" — don't conflate)
garage_type: null ("Niet aanwezig, wel mogelijk" -> no garage) | parking_type: "private" (private+public present, private is best)
insulation_raw: "Dakisolatie, HR-glas en muurisolatie"
heating_type: "gas_boiler" | boiler_year: 2013 | bathrooms: 1
neighborhood_avg_price_m2: null (Buurt section exists, this field genuinely absent)

LISTING 3: https://www.funda.nl/detail/koop/wijchen/huis-zevendreef-3079/44430595/
rooms: 6 | property_type: "Eengezinswoning, vrijstaande woning (split-level woning)" | year_built: 1992
energy_label: "C" | status: "Beschikbaar" | plot_size_m2: 266
ownership_type: "full" | erfpacht_canon_annual: null
garden_present: true | garden_type: "Tuin rondom" | garden_size_m2: null (no matching size field exists)
garden_orientation: null (no "Ligging tuin" field at all) | balcony_present: true
building_bound_outdoor_m2: 24 (also has separate "Overige inpandige ruimte 11 m²" — don't conflate)
garage_type: "carport" (appears under Garage heading here) | parking_type: "private" ("Op eigen terrein" only)
insulation_raw: "Dakisolatie, grotendeels dubbelglas en muurisolatie"
heating_type: "gas_boiler" | boiler_year: 2011 | bathrooms: 1
neighborhood_avg_price_m2: null (genuinely absent, same as Mill)

LISTING 4: https://www.funda.nl/detail/koop/aalsmeer/huis-zeeltstraat-19/80917766/
rooms: 6 | property_type: "Eengezinswoning, tussenwoning" | year_built: 2009
energy_label: "A" | status: "Beschikbaar" | plot_size_m2: 139
ownership_type: "full" | erfpacht_canon_annual: null
garden_present: true | garden_type: "Achtertuin" | garden_size_m2: 74
garden_orientation: "noordwesten" | balcony_present: null
building_bound_outdoor_m2: null (has "Externe bergruimte 6 m²" instead — do not map)
garage_type: null (no Garage subsection; has a Bergruimte/storage-shed section instead)
parking_type: "public" ("Openbaar parkeren" only)
insulation_raw: "Dubbel glas, HR-glas, muurisolatie en volledig geïsoleerd"
heating_type: "gas_boiler" (has partial underfloor heating too, not separately modeled)
boiler_year: 2023 | bathrooms: 1 | neighborhood_avg_price_m2: 5191
```

**Do not treat this as a one-shot check.** Fetch all 4 URLs, compare
every field against ground truth, and for any mismatch: diagnose the
specific cause using the structural facts above, fix it, then re-fetch
and re-compare ALL 4 listings again (a fix for one listing can break
another). Repeat this fetch-compare-fix cycle until every field matches
ground truth exactly on all four listings — including the fields that
should correctly be `null` (those are not failures, don't "fix" them
into having a value). Do not move to item 3 with any mismatch remaining,
and do not report success without showing the final matching output for
all 4 listings.

### 3. Fix scoring.py bugs (all confirmed via prior testing)

- **Audit ALL `_score_*` functions** for returning `None` — never `0` or
  any other default — when their required input fields are `None`. This
  was a systemic bug pattern (confirmed found in `_score_parking`
  specifically; audit the rest too, don't assume they're fine).
- **`_score_ownership` must be a 3-tier discrete model**, not a
  continuous formula: full ownership → `1.0`; erfpacht with no ongoing
  annual canon (paid off) → `0.7`; erfpacht with an ongoing annual canon
  → `0.3`. Return `None` only when `ownership_type` itself is `None`.
- **`_score_construction_condition` must actually combine both signals**:
  `(year_score + insulation_score) / 2` when both are present. The
  original bug computed both correctly as intermediates but returned only
  `year_score`, silently dropping insulation. When only one of the two is
  available, return that one alone (not `None`) — the criterion is only
  fully unavailable when both signals are missing.
- **Top-level renormalization algorithm is correct as originally
  designed** — confirmed via hand-verification in prior testing. Do not
  change it: when a criterion has no data, exclude it and redistribute
  its weight proportionally across the remaining available criteria;
  never score missing data as 0.

### 4. Add two new scoring criteria: `living_area` and `rooms`

Both follow the same None-on-missing-data pattern as every other
`_score_*` function.

**`_score_living_area(detail, filters: FilterConfig)`**
- floor = `filters.living_area_min` (read live from the already-merged
  `FilterConfig` — this dependency is no longer hypothetical, the
  co-worker's configurable-filter system is already in this codebase)
- cap: if a max living-area filter value exists, use it directly; if only
  a minimum is configured (current real case), cap = floor + 100; if no
  living-area filter is configured at all, return `None` for this
  criterion entirely (nothing principled to scale against)
- linear scale between floor (0.0) and cap (1.0), clamped to [0,1]

**`_score_rooms(detail, filters: FilterConfig)`**
- floor = `filters.bedrooms_min` (yes, bedrooms filter — not a bathrooms
  filter, confirmed intentional)); if no bedrooms filter configured,
  floor = 1
- cap = `max(8, floor + 4)`
- linear scale between floor (0.0) and cap (1.0), clamped to [0,1]

Do NOT add `price` or `year_built` as separate criteria — `price` is
already represented via `neighborhood_value`, `year_built` is already
inside `construction_condition`. Adding either again double-counts.

### 5. Final weights — 9 criteria (amenities removed)

Update `config/preferences.json`:

```json
{
  "weights": {
    "neighborhood_value": 21,
    "ownership": 17,
    "energy_label": 14,
    "living_area": 12,
    "construction_condition": 11,
    "parking": 8,
    "rooms": 7,
    "bathrooms": 6,
    "garden": 4
  }
}
```

Update `config/preferences-notes.md` as a dated revision (additive, keep
prior history): explain the 2 additions and their floor/cap-from-filter
design, and that bathrooms was kept at a modest weight despite testing
showing zero differentiation across the 4 sample listings (all scored
identically at 1 bathroom) — noted as a known limitation given small
sample size, not a reason to remove it.

### 6. Audit: full data reaching the scoring system

Confirm `main.py` merges the full card-scraped record (price,
living_area_m2, bedrooms, rooms, etc.) AND the detail-page dict into ONE
combined dict before calling `score_listing()` — required for
`neighborhood_value` (needs price/living_area) and the two new criteria.
Do not assume, verify explicitly.

### 7. Audit: detail fields actually persist to the DB, not just the score

Confirm the DB write for a scored listing includes the full detail dict
(`garden_size_m2`, `ownership_type`, `neighborhood_avg_price_m2`,
`insulation_raw`, etc. — all already columns from the original schema
migration), not just `score`/`score_breakdown`/`score_confidence`. This
matters concretely: if you retune `config/preferences.json`'s weights
later, rescoring should use already-stored data, not require re-fetching
every listing's detail page again (extra Funda requests to avoid, per
the anti-bot pacing rules).

### 8. Documentation sync (do throughout, not just at the end)

- `product.md` §12a: 9 criteria (not 8, not 10 — amenities removed,
  living area + rooms added), one-line descriptions matching existing
  style.
- `architecture.md`: Phase 2 section reflects the final schema (2 fewer
  columns — amenities dropped), the living_area/rooms scoring functions
  and their filter-config dependency, and the explicit confirmation that
  detail-fetch/scoring only happens at notify-time (see "Design
  confirmation" above) — this is worth stating plainly in the doc itself
  so a future session doesn't have to rediscover it.
- `operations.md`: no new run modes needed — `--dry-run` behavior is
  unchanged from before.
- `docs/site-notes/funda.md`: Learning Loop entries for whatever the
  actual root causes turn out to be during re-implementation (subsection
  reuse patterns, the two-text-format parsing issue, etc.) — don't just
  copy this document, write concise real entries as fixes land.

## Final verification, in order

1. Static: `config/preferences.json` weights sum to 100, imports clean.
2. Unit: `detail_scraper.py` against all 4 reference URLs, matches ground
   truth exactly (Step 2 above).
3. Unit: `scoring.py` with hand-crafted full/partial/all-None dicts,
   confirm renormalization math and the ownership/construction fixes.
4. Dry-run + SQLite inspection: confirm card fields populate for every
   scraped listing, and detail/score fields stay NULL for listings that
   haven't matched Phase 1 filters yet — only matching new/updated
   listings should show populated detail/score fields, confirming the
   notify-time-only gating from the Design confirmation section.
5. Live run forced-trigger + immediate re-run (dedup check): temporarily
   remove one known-matching row from the DB, run live, confirm detail
   fetch + score + Telegram notification all happen together in that one
   run; run again immediately, confirm no re-fetch/re-notify for that
   same listing.
