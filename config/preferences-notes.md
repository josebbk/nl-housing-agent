# Preference weights — derivation notes

Defaults below come from Joseph's ranked priority order (most → least
important): neighborhood value, construction condition, ownership,
energy label, amenities, garden, parking, bathrooms.

Weights were assigned by rank position, front-loaded toward the top few
(22/18/15/13/10/10/7/5, summing to 100). This is a starting point, not a
fixed formula — edit the numbers in preferences.json directly to retune;
this file just records the original reasoning so a future edit doesn't
lose the "why."

---

## 2026-08-16 — Expanded to 10 criteria, rebalanced weights

Two new criteria added after reviewing which scraped fields were unused in
scoring:

1. **living_area** (weight 11) — scores how far a listing's living area
   extends beyond the configured minimum. Thresholds documented in
   `living_area_thresholds` in preferences.json.
2. **rooms** (weight 7) — scores how many rooms a listing has relative to
   the configured bedrooms minimum. Thresholds documented in
   `rooms_thresholds` in preferences.json.

These fields were being scraped (card-level `rooms` from detail pages,
`living_area_m2` from both card and detail) but had no scoring function,
so they contributed nothing to the final score despite being available.

**Bathrooms note:** bathrooms was kept in the 10-criterion set despite
testing showing zero differentiation across the 4 sample listings
(all scored identically on bathrooms). The sample size was too small to
conclusively rule out the criterion — this is a known limitation, not
a reason to remove it.

No change to the top-level renormalization algorithm — it stays as-is
(confirmed correct in prior testing). The 10 weights sum to 100.

---

## 2026-08-16 — Removed amenities criterion (9 criteria now)

The `amenities` criterion was removed from the scoring system. The
keyword-matching extraction (`amenities_raw` from the "Voorzieningen"
field) proved unreliable and required disproportionate debugging effort
relative to the value it added to scoring — multiple sections on a Funda
detail page reuse the "Voorzieningen" label with different meanings,
requiring complex scoping heuristics that were fragile across listings.
The criterion (weight 5) was dropped entirely rather than continuing to
maintain it.

The remaining 9 weights were rebalanced to sum to 100:

| Criterion | Old | New |
|---|---|---|
| neighborhood_value | 20 | 21 |
| ownership | 16 | 17 |
| energy_label | 13 | 14 |
| living_area | 11 | 12 |
| construction_condition | 10 | 11 |
| parking | 8 | 8 |
| rooms | 7 | 7 |
| bathrooms | 6 | 6 |
| ~~amenities~~ | ~~5~~ | ~~removed~~ |
| garden | 4 | 4 |

The `amenities_tracked` keyword list was also removed from
`preferences.json`. The `amenities_raw` and `amenities_matched` database
columns were dropped (backed up to `data/funda.db.bak.amenities-removal`).

---

## 2026-08-20 — Expanded to 12 criteria, bathrooms removed, new formulas

`bathrooms` was removed from scoring (same finding as the amenities removal:
empirically non-discriminating across sampled real listings — all scored
identically on bathrooms).

Four new criteria were added using fields already extracted by the detail
scraper but unused by scoring:

1. **`garage`** (weight 6) — garage presence and type. The `garage_type`
   field was already being scraped from detail pages but had no scoring
   function. Funda omits the entire Garage section when no garage exists,
   so `garage_type` being `None` is treated as a confirmed negative.
2. **`plot_size`** (weight 5) — building plot size in m². The
   `plot_size_m2` field was scraped but unused. Linear "more is better"
   scoring.
3. **`balcony`** (weight 1) — balcony/dakterras presence. The
   `balcony_present` field was scraped but unused. Binary: present or not.
4. **`heating`** (weight 2) — heating type efficiency. The `heating_type`
   field was scraped but unused. Heat pump scores highest.

The following formulas were also revised:

- **Ownership:** changed from a flat 3-tier split (1.0 / 0.7 / 0.3) to a
  continuous scale: full = 1.0; erfpacht with no/zero canon = 0.8; erfpacht
  with a positive canon scales linearly to 0.0 at canon ≥ €1,000.
- **Energy label:** changed from a linear scale to a concave curve
  (sqrt of normalized index), so the score gap between low labels (G→F)
  is larger than between high labels (A+++→A++++).
- **Construction condition:** changed from 50/50 year-insulation weighting
  to 35% year / 65% insulation. Year bounds now sourced from
  `config/preferences.json` (`construction_year_range`) instead of being
  hardcoded.

Weight table change (old → new, or NEW / removed):

| Criterion | Old weight | New weight |
|---|---|---|
| neighborhood_value | 21 | 20 |
| ownership | 17 | 15 |
| energy_label | 14 | 13 |
| living_area | 12 | 11 |
| construction_condition | 11 | 10 |
| parking | 8 | 7 |
| rooms | 7 | 6 |
| ~~bathrooms~~ | ~~6~~ | ~~removed~~ |
| garden | 4 | 4 |
| ~~amenities~~ | ~~removed~~ | ~~removed~~ |
| **garage** | — | **6** (NEW) |
| **plot_size** | — | **5** (NEW) |
| **balcony** | — | **1** (NEW) |
| **heating** | — | **2** (NEW) |

Total: 100 points across 12 criteria.

### Coverage safeguard (partial_major_missing)

A fourth confidence value was added to `score_listing()`: `"partial_major_missing"`.
This is set when the single highest-weighted criterion (determined
dynamically from the weight table, not hardcoded to a specific name) is
among the missing criteria. It signals a stronger low-confidence case than
an ordinary partial score, since the criterion carrying the most weight
could not be evaluated. This is a confidence-label-only change — it does
not alter how the numeric score is computed.

### Breakdown reconciliation

The breakdown's `points_possible` and `points_earned` values are now
computed on the same renormalized scale as the final score, so they
always sum correctly even when some criteria are missing. Previously
they used un-renormalized raw weights.

### Weight-sum validation

`_load_preferences()` now raises a `ValueError` (instead of logging a
warning) if weights in `config/preferences.json` do not sum to exactly
100.