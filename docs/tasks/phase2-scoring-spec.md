# Phase 2 Task Spec — Detail-Page Scraping & Preference Scoring

**Scope owner:** Joseph (this task only). Configurable-filter work in `storage.py`
(schema/table-level dedup logic already present) and `main.py`'s filter
selection is being developed separately by a co-worker — do not change
filter *logic* itself, only add the new columns/fields and the new
scoring call site described below.

**Do not touch:** `scraper.py`'s existing card-scraping logic, the existing
Phase 1 filter thresholds, `notifier.py`'s core send function signature
(only extend the message body it's given).

---

## 1. New files

### `src/detail_scraper.py`
Fetches and parses a single Funda listing detail page.

```python
def fetch_listing_details(url: str) -> dict:
    """
    Reuses the existing urllib-headers -> data: URL -> Playwright rendering
    technique already implemented in scraper.py (same Akamai bypass).
    Returns a dict with keys listed in Section 3. Any field that cannot be
    parsed must be set to None, never omitted, never guessed.
    Must not raise on missing/absent sections — absence is expected and normal.
    """
```

Reuse the existing single-browser-instance / pacing rules from `scraper.py`.
Do not open a second concurrent browser instance.

### `src/scoring.py`

```python
def score_listing(detail: dict, preferences: dict) -> ScoreResult:
    """
    ScoreResult fields: score (int 0-100 or None), breakdown (list of
    {criterion, points_earned, points_possible, label}), confidence
    ("full" | "partial" | "no_data"), missing_criteria (list[str]).
    See Section 4 for the renormalization algorithm.
    """
```

### `config/preferences.json`
Hand-editable weights + keyword dictionaries (Section 5).

---

## 2. Why detail-page parsing is keyword-based, not enum-based

Verified against 3 real listings (Amsterdam, Mill, Wijchen): fields like
`Voorzieningen`, `Isolatie`, `Soort garage`, and `Soort parkeergelegenheid`
are **free text written by individual listing agents**, not a fixed set of
values. `Voorzieningen` alone produced three completely non-overlapping
lists across three listings. Do not attempt exact-match lookups against
these fields. Use substring/keyword matching against the dictionaries in
`config/preferences.json`, case-insensitive, and ignore anything not in the
dictionary rather than failing.

Fields confirmed genuinely structured (safe for direct parsing):
`Energielabel` (ordinal, A++++→G), `Bouwjaar` (int), `Vraagprijs`,
`Vraagprijs per m²`, all `Oppervlakten` fields, `Aantal kamers`/
`Aantal badkamers` (consistent phrasing, regex-safe).

---

## 3. Fields to extract (all nullable — never fabricate a value)

| Field | Source (Kenmerken heading) | Extraction approach |
|---|---|---|
| `ownership_type` | Kadastrale gegevens → Eigendomssituatie | If **any** cadastral parcel's value contains "erfpacht" → `"erfpacht"`. Else if any contains "volle eigendom" → `"full"`. Else `None`. Listings can have multiple parcels — check all of them. |
| `erfpacht_canon_annual` | same section, nearby "Lasten" / "€.../jaar" text | regex for `€\s*[\d.,]+` near "jaar"/"jaarlijks"; `None` if absent |
| `garden_present` | Buitenruimte → Tuin (field presence) | `True` if a "Tuin" field exists at all, regardless of size being given |
| `garden_type` | same | raw value string, e.g. "Achtertuin", "Tuin rondom" |
| `garden_size_m2` | same, or dedicated "Achtertuin" sub-field | regex `(\d+)\s*m²`; `None` if no number present (confirmed this happens — Wijchen listing has no garden size) |
| `garden_orientation` | "Ligging tuin" | keyword match: noorden/oosten/zuiden/westen/combinations; `None` if absent |
| `balcony_present` | "Balkon/dakterras" field | `True` if field present and contains "aanwezig" |
| `building_bound_outdoor_m2` | Oppervlakten → "Gebouwgebonden buitenruimte" | regex number; distinct from garden, do not conflate |
| `garage_type` | "Soort garage" | keyword classify: "niet aanwezig" → `none`; "aangebouwd"/"inpandig" → `attached`; "vrijstaand" → `detached`; "carport" → `carport` (note: one sample listing put "Carport" under this heading instead of Parkeergelegenheid — check both) |
| `parking_type` | "Soort parkeergelegenheid" | keyword classify, priority order: "eigen terrein" → `private`; "carport" → `carport`; "openbaar" → `public`; "betaald" → `paid`; combine flags if multiple present, use the best one found |
| `insulation_raw` | "Isolatie" | raw string, keep as-is |
| `insulation_score` | derived from `insulation_raw` | count of {dak/vloer/muur/spouwmuur}-isolatie keywords present (cap 3) combined with glass-quality tier keyword (hr++ > hr+ > hr-glas > dubbel glas/dubbelglas > enkel glas > none found); see Section 4 for exact weighting |
| `heating_type` | "Verwarming" | keyword: "warmtepomp" → `heat_pump`; "stadsverwarming"/"blokverwarming" → `district`; "cv-ketel" → `gas_boiler`; else `None` |
| `boiler_year` | "Cv-ketel" descriptive text | regex `uit (\d{4})`; `None` if absent |
| `amenities_raw` | "Voorzieningen" | raw comma list |
| `amenities_matched` | derived | list of dictionary keywords found (case-insensitive substring) |
| `bathrooms` | "Aantal badkamers" | regex `(\d+)\s*badkamer` |
| `neighborhood_avg_price_m2` | Buurt section "Gem. vraagprijs / m²" | plain number; **confirmed absent on 2 of 3 sample listings** — must be `None`-safe |
| `detail_fetched_at` | n/a | ISO 8601 timestamp of fetch |

---

## 4. Scoring algorithm (renormalization for missing data)

```python
def score_listing(detail, preferences):
    weights = preferences["weights"]  # 8 criteria, sums to 100 by default
    subscores = {
        "neighborhood_value": _score_neighborhood_value(detail, preferences),
        "construction_condition": _score_construction(detail, preferences),
        "ownership": _score_ownership(detail),
        "energy_label": _score_energy_label(detail, preferences),
        "amenities": _score_amenities(detail, preferences),
        "garden": _score_garden(detail, preferences),
        "parking": _score_parking(detail),
        "bathrooms": _score_bathrooms(detail),
    }  # each value is a float in [0,1] or None if data unavailable

    available = {k: v for k, v in subscores.items() if v is not None}
    missing = [k for k in subscores if subscores[k] is None]

    if not available:
        return ScoreResult(score=None, breakdown=[], confidence="no_data", missing_criteria=missing)

    total_weight = sum(weights[k] for k in available)
    score = round(sum(weights[k] * available[k] for k in available) / total_weight * 100)
    confidence = "full" if not missing else "partial"

    breakdown = [
        {
            "criterion": k,
            "points_earned": round(weights[k] * available[k]) if k in available else 0,
            "points_possible": weights[k],
            "matched": k in available,
        }
        for k in weights
    ]
    return ScoreResult(score=score, breakdown=breakdown, confidence=confidence, missing_criteria=missing)
```

Each `_score_*` function returns `None` (not 0) when its underlying detail
fields are unavailable — this is the signal that triggers renormalization.
Returning 0 for missing data would unfairly punish a listing just because
Funda's listing agent wrote less detail; `None` means "we don't know,"
0 means "we know, and it's bad."

`neighborhood_avg_price_m2` is the most likely field to be `None` (confirmed
missing on 2 of 3 real listings) — this is expected, not a bug.

---

## 5. `config/preferences.json` — initial version

```json
{
  "weights": {
    "neighborhood_value": 22,
    "construction_condition": 18,
    "ownership": 15,
    "energy_label": 13,
    "amenities": 10,
    "garden": 10,
    "parking": 7,
    "bathrooms": 5
  },
  "energy_label_scale": ["G","F","E","D","C","B","A","A+","A++","A+++","A++++"],
  "amenities_tracked": [
    "airconditioning", "alarminstallatie", "buitenzonwering",
    "tv kabel", "glasvezelkabel", "mechanische ventilatie", "rolluiken"
  ],
  "insulation_keywords": {
    "components": ["dakisolatie", "vloerisolatie", "muurisolatie", "spouwmuurisolatie"],
    "glass_tiers": ["enkel glas", "dubbel glas", "dubbelglas", "hr-glas", "hr glas", "hr+", "hr++"]
  },
  "neighborhood_value_thresholds": { "good_ratio": 0.8, "bad_ratio": 1.2 },
  "garden_size_cap_m2": 50,
  "garden_orientation_bonus": ["zuiden", "westen"]
}
```

Weights must sum to 100 by convention (not enforced in code, but validate
and log a warning on startup if they don't).

### Companion doc: `config/preferences-notes.md`

JSON can't hold comments, so also generate this short markdown file
alongside `preferences.json`, explaining where the default weights came
from, e.g.:

```markdown
# Preference weights — derivation notes

Defaults below come from Joseph's ranked priority order (most → least
important): neighborhood value, construction condition, ownership,
energy label, amenities, garden, parking, bathrooms.

Weights were assigned by rank position, front-loaded toward the top few
(22/18/15/13/10/10/7/5, summing to 100). This is a starting point, not a
fixed formula — edit the numbers in preferences.json directly to retune;
this file just records the original reasoning so a future edit doesn't
lose the "why."
```

---

## 6. Schema changes — `storage.py`

Co-worker has not started on `storage.py` yet — these columns can be added
now. Use `ALTER TABLE listings ADD COLUMN ...` (do not drop/recreate the
table — `data/funda.db` already has seeded production data per
`operations.md` §5). **Back up `data/funda.db` before running the migration**,
even though this is authorized — it's still a schema change to live data.

New nullable columns on `listings`:

```
ownership_type TEXT
erfpacht_canon_annual REAL
garden_present INTEGER
garden_type TEXT
garden_size_m2 INTEGER
garden_orientation TEXT
balcony_present INTEGER
building_bound_outdoor_m2 INTEGER
garage_type TEXT
parking_type TEXT
insulation_raw TEXT
insulation_score REAL
heating_type TEXT
boiler_year INTEGER
amenities_raw TEXT
amenities_matched TEXT        -- JSON-encoded list
bathrooms INTEGER
neighborhood_avg_price_m2 REAL
score INTEGER
score_breakdown TEXT          -- JSON-encoded list
score_confidence TEXT         -- "full" | "partial" | "no_data"
detail_fetched_at TEXT
```

---

## 7. Integration point — `main.py`

Joseph has confirmed `main.py` is free to edit for this task. Co-worker's
separate configurable-filter work will also touch `main.py` later, so keep
this change as a small, clearly isolated block (not woven through the
existing filter logic) so it's easy for a future diff to merge cleanly
around it, per `AGENTS.md`'s "prefer additive changes" guidance.

The integration point, conceptually — inserted after Phase 1 filtering,
before the notification-queue step:

```python
for listing in newly_matching_listings:   # already new/updated AND passed Phase 1 filters
    detail = fetch_listing_details(listing["url"])
    result = score_listing(detail, preferences)
    # persist detail fields + result.score/breakdown/confidence to the row
    # pass result into the notification builder
```

Only listings that are **new or updated AND already pass the Phase 1
hard filters** get a detail-page fetch — this bounds the extra request
volume to exactly the listings that would trigger a notification anyway,
per the anti-bot pacing rules in `AGENTS.md`/`operations.md`.

---

## 8. Notification format — `notifier.py`

```
🏠 {address} — €{price} — {living_area}m² — {bedrooms} bed
Score: {score}/100{confidence_flag}
{breakdown lines, one per criterion, ✓/✗ prefix, "(+earned/possible)"}
🔗 {url}
```

`confidence_flag` is `" ⚠ partial data ({missing criteria list})"` when
`confidence == "partial"`, and an empty string when `confidence == "full"`.
When `confidence == "no_data"`, still send the notification (it already
passed Phase 1 filters) but show `Score: unavailable` instead of a number.

---

## 9. Documentation updates required (same task, per `AGENTS.md`)

### `product.md`
- Add a new section documenting that ranking/scoring (previously a Phase 2
  roadmap bullet in §12) is now active, with a plain-language description
  of the methodology: user-weighted criteria, 0–100 score, graceful
  degradation on missing data, confidence flag. Do not put the exact
  weight numbers here — those live in `config/preferences.json` and may
  change without being a product-scope change.

### `architecture.md`
- New section: "Detail-Page Scraping & Scoring" describing the two new
  modules, the new data flow step (Phase 1 filter match → detail fetch →
  score → notify), the schema additions from Section 6 above, and the
  keyword-dictionary-over-enum design decision with the reasoning from
  Section 2 (cite the 3-listing verification).
- Update "Open Decisions" §4 ("Phase 2 filter configuration") to note that
  *ranking/scoring* configuration is now resolved via `config/preferences.json`,
  while filter-threshold configuration remains the co-worker's open item —
  keep these explicitly distinct so a future reader doesn't conflate them.

### `operations.md`
- Add a verification step for this feature, mirroring the existing Step 5
  sequence: dry-run review of scored output (confirm breakdown math and
  renormalization look right on real listings), confirm the confidence
  flag appears correctly on listings with missing neighborhood data, then
  a live run before considering this operationally verified.

---

## 10. Explicitly out of scope for this task

- Changing Phase 1 hard filter thresholds or logic (co-worker's work).
- Building a UI/CLI for editing `preferences.json` (hand-edit only, for now).
- Any new dependency — everything above uses `re`, `json`, and the existing
  urllib/Playwright stack already in `scraper.py`.
