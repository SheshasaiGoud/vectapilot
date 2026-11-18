# data/safety — the guard term files (M7 PR 4)

> **Synthetic, agent-drafted, not professionally reviewed (Q8).** shesha reviews every term
> (`docs/notes/m7-term-review.md`). Not for a real business until Q8 and Q4c are answered.

The guards' configuration folder (`VECTAPILOT_GUARD_CONFIG_DIR`, G4; loader:
`apps/api/src/vectapilot_api/guards/config.py`). `infra/compose.yaml` mounts it read-only.

- `platform.json`: every business. Distress terms and a few physical emergencies
  (unconscious, not breathing, choking, severe bleeding, a swallowed battery or poison: §3.5
  applies to every business type), injection phrases (G7), output phrases
  (success claims and medical advice, G8; mild abuse, D2 (i)), script ranges for D3 (i)'s
  uncovered-language rule, and the fixed negatives of the self-test (G14).
- `types/<business_type>.json`: each health type's red flags (§4.1, §4.2) mapped to M4's
  `emergency` or `professional_judgement`, its diagnosis phrases, and
  `not_covered_categories: ["self_harm"]` (no crisis terms by agents, D6).
- `businesses/<business_id>.json`: a business adds terms, never removes one. It carries its
  guidance (each line verbatim from its own `data/sample/<id>/emergency.md`, equal to its
  `tools.json`) and its `not_covered` languages.

Every term cites a business-context line, a synonym rule ending at one, or (injection and output
only) a rule of the plan; never a frozen red-team case. Romanised Hindi is in the `hi` lists,
matched as written. No Telugu terms until a Telugu reviewer exists (D6). The lists are public on
purpose: the screen's value is recall, failing closed and M4's router, not secrecy.

**Authoring rules** (a test pins the first two):
- Every red-flag `near` term has a window of at least 5 words (the red-team README's rule, stated
  before this PR), and its word sets carry inflections: matching is strict whole words, so a
  missing "pains", "fainting" or "नही" is a silent miss.
- "Within N words" in a source rule means `window: N`.
- An ingestion verb and its object use a window of 7: the object can carry a determiner, a
  quantity and adjectives ("ate half a bag of dark chocolate").
- A word that is also an everyday word ("fits", "acid", "took", "forget") only counts next to a
  word that makes it a red flag or an injection.

The fixed negatives (G14) are new look-alikes, not paraphrases of the frozen category-L cases.

Tests: `apps/api/tests/guards/test_safety_terms.py`; with H1, G14's self-test in `test_match.py`.

## Injection terms that cite a G7 class (decided 2026-11-18, shesha)
An injection term may cite an author-written G7 injection class (the plan's G7 surfaces) as its
source, not only a business-context line. Such a term can mirror a frozen red-team case (for
example `pf_note_to_the_assistant` and b-007), so the frozen suite's injection number is not
independent of the term files; the held-out probe (D6 (iii)) is the independent injection figure,
and the M7 note says so.
