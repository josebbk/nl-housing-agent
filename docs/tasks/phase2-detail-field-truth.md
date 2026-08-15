# Phase 2 — Detail Page Field Location Map & Ground Truth

This is a test oracle, not a design doc. Use it to verify `detail_scraper.py`
against real pages field-by-field, not just "does it crash."

**Note on "addresses":** these are semantic paths (Kenmerken subsection
heading → field label), not literal CSS selectors — I don't have access to
the raw HTML/DOM, only rendered page text. Resolve each path to the actual
selector by inspecting the live rendered DOM directly; the heading/label
text below is what's stable across all four listings and should anchor
whatever selector you use.

---

## Part 1 — Field location map (applies to all listings)

| Schema field | Location (Kenmerken subsection → label) | Notes |
|---|---|---|
| `rooms` | `Indeling` → `Aantal kamers` | leading number before "kamers" |
| `property_type` | `Bouw` → `Soort woonhuis` | free text, store as-is |
| `year_built` | `Bouw` → `Bouwjaar` | plain int |
| `energy_label` | `Energie` → `Energielabel` | **not** the bare letter shown in the icon-stat row near the top of the page — that row has no reliable label attached to it, don't source from there |
| `status` | `Overdracht` → `Status` | |
| `plot_size_m2` | `Oppervlakten en inhoud` → `Perceel` | |
| `ownership_type` | `Kadastrale gegevens` → `Eigendomssituatie` | **can repeat once per cadastral parcel** (a listing can have 2+ parcels, e.g. "AMSTERDAM AL 394" and "AMSTERDAM AL 395" as separate blocks, each with its own `Eigendomssituatie`). If **any** parcel's value contains "erfpacht" → `"erfpacht"`. Else if any contains "volle eigendom" → `"full"`. |
| `erfpacht_canon_annual` | `Kadastrale gegevens` → `Lasten` | sibling field to `Eigendomssituatie`, only appears on parcels that are erfpacht with an ongoing canon. Absent = full ownership or paid-off erfpacht → `None`. |
| `garden_present` / `garden_type` | `Buitenruimte` → `Tuin` | presence of this field at all = `garden_present=True`; its value (e.g. "Achtertuin", "Tuin rondom") = `garden_type` |
| `garden_size_m2` | `Buitenruimte` → **a second field whose label matches `garden_type`'s value** | e.g. if `Tuin` = "Achtertuin", look for a separate field literally labeled `Achtertuin` holding "76 m² (8,00 meter diep en 9,54 meter breed)". **This field does not always exist** — confirmed absent on one of the four reference listings even though `Tuin` was present. Extract the m² number and, if present, the width/depth in parentheses. `None` if the matching field isn't there. |
| `garden_orientation` | `Buitenruimte` → `Ligging tuin` | **also not always present** — confirmed absent on one reference listing. `None` if missing, don't guess from `Tuin`'s value. |
| `balcony_present` | `Buitenruimte` → `Balkon/dakterras` | field only appears on some listings; `True` if present and says "aanwezig". Absent field = `None`, not `False`. |
| `building_bound_outdoor_m2` | `Oppervlakten en inhoud` → `Gebouwgebonden buitenruimte` | **Do not confuse with two other, different fields that can appear in the same subsection:** `Overige inpandige ruimte` (indoor storage space, not outdoor) and `Externe bergruimte` (external storage). One reference listing has both `Overige inpandige ruimte` AND `Gebouwgebonden buitenruimte` present simultaneously with different values — only `Gebouwgebonden buitenruimte` maps to this column. |
| `garage_type` | `Garage` → `Soort garage` | whole `Garage` subsection is absent when there's no garage at all (→ `None`, not a special value). When present, classify by keyword: "aangebouwd"/"inpandig" → `attached`; "vrijstaand" → `detached`; "carport" → `carport`; "niet aanwezig" → `none`. |
| `parking_type` | `Parkeergelegenheid` → `Soort parkeergelegenheid` | can contain multiple space-separated values ("Betaald parkeren en openbaar parkeren"); classify by keyword priority: "eigen terrein" (best) > "carport" > "openbaar" > "betaald" (worst); pick the best keyword actually present |
| `insulation_raw` | `Energie` → `Isolatie` | free text, keyword-score downstream in scoring.py, don't classify here |
| `heating_type` | `Energie` → `Verwarming` | keyword: "warmtepomp" → `heat_pump`; "stadsverwarming"/"blokverwarming" → `district`; "cv-ketel" → `gas_boiler` |
| `boiler_year` | `Energie` → `Cv-ketel` | free text, regex `uit (\d{4})` |
| `amenities_raw` | `Indeling` → `Voorzieningen` **only** | **This label is reused in at least THREE different subsections with different meanings**: `Indeling` (living-space amenities — the one we want), `Garage` (garage electricity/water), and `Bergruimte` (storage shed utilities). Scope positively to "must be inside `Indeling`" — don't just exclude `Garage`, since `Bergruimte` also has one. |
| `bathrooms` | `Indeling` → `Aantal badkamers` | regex leading number before "badkamer" |
| `neighborhood_avg_price_m2` | **top-level `Buurt` section — outside the `Kenmerken` container entirely**, sibling to it, not nested inside | → `Gem. vraagprijs / m²`. **Confirmed absent on 2 of the 4 reference listings** (smaller/rural areas) — must be `None`-safe, not an extraction failure. |

---

## Part 2 — Ground truth per listing

### Listing 1 — Hilversumstraat 60, Amsterdam
`https://www.funda.nl/detail/koop/amsterdam/huis-hilversumstraat-60/44480057/`

```
rooms: 4
property_type: "Eengezinswoning, hoekwoning"
year_built: 1969
energy_label: "C"
status: "Beschikbaar"
plot_size_m2: 163
ownership_type: "erfpacht"          # both AL 394 and AL 395 parcels are Gemeentelijke erfpacht
erfpacht_canon_annual: 408.85       # from AL 394's "Lasten € 408,85 per jaar"; AL 395 has no Lasten line
garden_present: true
garden_type: "Achtertuin"
garden_size_m2: 76                  # "76 m² (8,00 meter diep en 9,54 meter breed)"
garden_orientation: "zuiden"        # "Gelegen op het zuiden bereikbaar via achterom"
balcony_present: null               # no Balkon/dakterras field on this listing
building_bound_outdoor_m2: null     # this listing has "Overige inpandige ruimte 20 m²" instead — do NOT map that to this field
garage_type: "attached"             # "Aangebouwde stenen garage"
parking_type: "paid+public"         # "Betaald parkeren en openbaar parkeren" — no "eigen terrein"
insulation_raw: "Dakisolatie, gedeeltelijk dubbel glas en muurisolatie"
heating_type: "gas_boiler"
boiler_year: 2011                   # "Nefit (gas gestookt combiketel uit 2011, eigendom)"
amenities_raw: "Airconditioning, alarminstallatie, buitenzonwering, schuifpui, en TV kabel"
amenities_matched: ["airconditioning","alarminstallatie","buitenzonwering","tv kabel"]   # 4, "schuifpui" not tracked
bathrooms: 1
neighborhood_avg_price_m2: 5216
```

### Listing 2 — Mergen 20, Mill
`https://www.funda.nl/detail/koop/mill/huis-mergen-20/80918937/`

```
rooms: 7
property_type: "Eengezinswoning, 2-onder-1-kapwoning"
year_built: 1993
energy_label: "A"
status: "Beschikbaar"
plot_size_m2: 244
ownership_type: "full"              # "Volle eigendom"
erfpacht_canon_annual: null
garden_present: true
garden_type: "Achtertuin en voortuin"
garden_size_m2: 76                  # field labeled "Achtertuin" (not the full "Achtertuin en voortuin" string) — "76 m² (8,00 meter diep en 9,50 meter breed)"
garden_orientation: "noordwesten"
balcony_present: null               # no Balkon/dakterras field
building_bound_outdoor_m2: 20       # "Gebouwgebonden buitenruimte 20 m²" — distinct from "Overige inpandige ruimte 23 m²", which also exists on this listing but is NOT this field
garage_type: null                   # "Niet aanwezig, wel mogelijk" -> classify as no garage (optionally track "possible" as a separate note, but garage_type itself = none/null)
parking_type: "private+public"      # "Op eigen terrein en openbaar parkeren"
insulation_raw: "Dakisolatie, HR-glas en muurisolatie"
heating_type: "gas_boiler"
boiler_year: 2013                   # "Remeha Advanta 35 C (gas gestookt combiketel uit 2013, eigendom)"
amenities_raw: "Glasvezelkabel, mechanische ventilatie, en rolluiken"
amenities_matched: ["glasvezelkabel","mechanische ventilatie","rolluiken"]   # 3
bathrooms: 1
neighborhood_avg_price_m2: null     # Buurt section exists (Inwoners/Gezin met kinderen present) but has NO "Gem. vraagprijs / m²" field at all — genuinely absent, not a bug if null
```

### Listing 3 — Zevendreef 3079, Wijchen
`https://www.funda.nl/detail/koop/wijchen/huis-zevendreef-3079/44430595/`

```
rooms: 6
property_type: "Eengezinswoning, vrijstaande woning (split-level woning)"
year_built: 1992
energy_label: "C"
status: "Beschikbaar"
plot_size_m2: 266
ownership_type: "full"              # "Volle eigendom"
erfpacht_canon_annual: null
garden_present: true
garden_type: "Tuin rondom"
garden_size_m2: null                # NO field labeled "Tuin rondom" with a size exists on this listing — confirmed genuinely absent, don't fabricate a value
garden_orientation: null            # NO "Ligging tuin" field on this listing at all — also genuinely absent
balcony_present: true               # "Balkon/dakterras: Balkon aanwezig"
building_bound_outdoor_m2: 24       # "Gebouwgebonden buitenruimte 24 m²" — this listing ALSO has "Overige inpandige ruimte 11 m²" as a separate, different field; do not conflate the two
garage_type: "carport"              # "Soort garage: Carport" — note "Carport" appears under the Garage heading here, not Parkeergelegenheid
parking_type: "private"             # "Op eigen terrein" only
insulation_raw: "Dakisolatie, grotendeels dubbelglas en muurisolatie"
heating_type: "gas_boiler"
boiler_year: 2011                   # "Atag (gas gestookt combiketel uit 2011, eigendom)"
amenities_raw: "Glasvezelkabel en TV kabel"
amenities_matched: ["glasvezelkabel","tv kabel"]   # 2
bathrooms: 1
neighborhood_avg_price_m2: null     # Buurt section exists but no "Gem. vraagprijs / m²" field, same as Mill
```

### Listing 4 — Zeeltstraat 19, Aalsmeer
`https://www.funda.nl/detail/koop/aalsmeer/huis-zeeltstraat-19/80917766/`

```
rooms: 6
property_type: "Eengezinswoning, tussenwoning"
year_built: 2009
energy_label: "A"
status: "Beschikbaar"
plot_size_m2: 139
ownership_type: "full"              # "Volle eigendom"
erfpacht_canon_annual: null
garden_present: true
garden_type: "Achtertuin"
garden_size_m2: 74                  # "74 m² (13,50 meter diep en 5,50 meter breed)"
garden_orientation: "noordwesten"
balcony_present: null               # no Balkon/dakterras field
building_bound_outdoor_m2: null     # this listing has "Externe bergruimte 6 m²" instead — a THIRD distinct field, do not map to this column
garage_type: null                   # no Garage subsection at all on this listing (has a Bergruimte/storage-shed section instead)
parking_type: "public"              # "Openbaar parkeren" only, no "eigen terrein"
insulation_raw: "Dubbel glas, HR-glas, muurisolatie en volledig geïsoleerd"
heating_type: "gas_boiler"          # "Cv-ketel en gedeeltelijke vloerverwarming" — classify as gas_boiler; partial underfloor heating isn't separately modeled, don't invent a new category for it
boiler_year: 2023                   # "Intergas HRE 36/30 CW (gas gestookt combiketel uit 2023, eigendom)"
amenities_raw: "Dakraam, glasvezelkabel, mechanische ventilatie, en TV kabel"
amenities_matched: ["glasvezelkabel","mechanische ventilatie","tv kabel"]   # 3, "dakraam" not tracked
bathrooms: 1
neighborhood_avg_price_m2: 5191
```

---

## Part 3 — Instructions for CLI

1. **Scope:** only edit `src/detail_scraper.py` (and, if genuinely required by a
   fix, small supporting helpers it calls). Do not touch `scraper.py`'s
   card-level scraping, Phase 1 filter logic, `scoring.py`, or anything in
   `main.py`/`storage.py` — none of that is relevant to this task.

2. Run `fetch_listing_details()` against all four URLs in Part 2. For each
   listing, compare every field against its ground truth above, field by
   field — not just "did it return without error."

3. For every mismatch: identify which field, what was returned vs. expected,
   and fix the extraction logic using Part 1's location map. Common causes
   to check first, based on issues already found in this project: wrong
   subsection scoped (e.g. reading a reused label from the wrong container),
   assuming a fixed field name where the real field name is dynamic (the
   garden-size case), or conflating two different fields with similar names
   (the three outdoor/indoor-space fields case).

4. Re-run against all four listings after each fix. Repeat until every
   field matches ground truth on all four listings, including the fields
   that are correctly expected to be `null`/`None` — a listing returning
   `None` for a field that's genuinely absent on the page is CORRECT, not
   a failure; don't "fix" those.

5. Update `docs/site-notes/funda.md` with a Learning Loop entry per
   `AGENTS.md`, covering whatever mismatches were actually found and fixed
   (not a copy of this whole document — a concise symptom/diagnosis/fix
   entry per genuine bug). No changes needed to `product.md` or
   `architecture.md` unless you discover something that changes the actual
   architecture (e.g. a field that doesn't exist at all and needs a design
   decision) — if that happens, stop and flag it instead of deciding
   unilaterally.

6. **When finished, report:** the final field-by-field output for all four
   listings (not a summary claim — show the actual returned dict for each),
   a list of what was actually wrong and what you changed to fix it, and
   confirmation that all four now match ground truth exactly.
