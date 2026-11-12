# Extraction configuration: synthetic and fictional (M6)

**Everything under `data/extraction/` is synthetic configuration.** The businesses are the
fictional ones of [`data/sample/`](../sample/README.md) (business-context §7). The templates and
business files were written by Claude (ananya-rag) for M6 PR 1 on 2026-11-11, following
[ADR 0017](../../docs/adr/0017-document-extraction.md) decision 2 and the plan's PR 1
(`docs/plans/m6-pr-checks.md`). No real person, patient, pet, doctor, insurer or policy is here,
and this folder holds no document: the documents and their gold values come with PR 4's
`doc_extraction` set.

## Layout

```
data/extraction/
  templates/<business_type>/<kind>.json   one document kind for one business type
  businesses/<business_id>.json           one business's extraction settings
```

The API reads this folder from `VECTAPILOT_EXTRACTION_CONFIG_DIR` (under `infra/compose.yaml`, a
read-only bind mount at `/app/data/extraction`). When the setting is given, every file must be
valid, or the API does not start (`apps/api/src/vectapilot_api/extraction/`).

- **A template** (`config.Template`): `business_type` and `kind` (both equal to its path), a
  version, a title and a description (prompt text, reviewed), the date formats a business may
  choose from, at most 8 fields, the fields always sent to review, and cross-field rules. Each
  field has a type (`text`, `date`, `enum`, `identifier`), a category (`identity`, `contact`,
  `administrative`; there is no clinical category), a description, 1-5 printed `labels`, and the
  rules `required` and `name`; `after` is a cross rule between two date fields. A label may not
  appear inside another field's label (`Date` inside `Date of birth` would name the wrong field).
- **A business file** (`config.BusinessFile`): `synthetic: true`, its `business_type` (must equal
  its corpus manifest's), `retention_days` (1-90, default 30), and the enabled `kinds`, each with
  its date formats (1-3, from the template's), an optional narrower list for an enum field, and
  extra always-review fields. It names no template: the template is chosen by the business's type
  and the kind, so a veterinary business can never select a human-healthcare template.

## Tighten, never loosen (E5)

A business file can add always-review fields, narrow an enum to some of the template's ids and
pick its date formats from the template's. It has no key to remove a field, a label, a rule or an
always-review field, and anything it does say is checked against its template. A set of formats
with both day-first and month-first numeric dates (`DD/MM/YYYY` with `MM/DD/YYYY`) is refused,
because `03/04/2026` would read as two different dates.

## What the templates ask for (E6, D6 (a))

- `human_healthcare/referral_letter`: patient name, date of birth, referring doctor, referral
  date, insurer, policy number (always reviewed). No referral reason and no requested service:
  those are health data, and the requested service waits for business-context Q7.
- `veterinary/vaccination_record`: pet name, species, owner name, vaccine (a type-level list),
  date given, valid until (after the date given), issuing clinic. Paws & Co narrows species to
  dogs, cats and rabbits, the animals its synthetic `services.html` vaccinates.
