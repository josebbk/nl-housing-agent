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