"""The `redteam` suite (M7 PR 2; D6 (a), D7 (b), D8 (a)): the frozen adversarial cases' tools.

Dataset commands (no model, no network, no database; `cli.py`, run as
`uv run python -m vectapilot_api.redteam`):
- `cases` — the case schema (every field of the scoring rules, closed vocabularies), the
  manifest, the gate labels and their loaders
- `pii` — `{{pii:<type>}}` placeholders, the seeded standard-library generator that fills them at
  run time from reserved or fictional ranges, and the literal shape checks
- `checks` — one stage-aware check (placeholders, literals, languages, businesses, fixtures per
  surface, gate labels) and the freeze
- `freeze` — what `freeze.json` pins, and the rule that gate labels only tighten
- `report` — counts (and a line of codes per case); never a case's text
- `files` — reading the version, its businesses and `fixtures/`, writing a freeze

The harness:
- `scoring` (outcome *and* deciding rule, the positive controls, `ExpectationMiss`,
  `PendingHandWrite`, `NotScored`), `plan` (each case's path, the PR or hand-write it waits for,
  its environment and CI mark), `harness` (`run_case`, shared by pytest and `run`, and the runner
  registry later PRs fill), `masking` (category E's runner, M7 PR 3), `replay` (the scripted
  stand-in for M5's H1 `run_turn`, not a loop),
  `businesses` (`rt-shop` from the dataset fixtures, as a served app reads it)
- `runinfo` (folder names, run.json and its fingerprint), `spend` (D7's $1.00 on M3's counting
  core), `ledger` (once per config), `runs` (`run` and `summarize`), `summary` (results lines and
  summary.md)

The cases are agent-drafted and synthetic (D6 (a)), frozen before PR 4's term files; the held-out
probe is separate, and agents never read it. The dataset is `evals/datasets/redteam/<version>/`;
its README holds the authoring rules. The suite scores the guards from outside: of
`vectapilot_api.guards` it imports only the closed codes (`types`), `match_terms` and `mask_pii`
to probe whether H1 and H2 are written (`plan`), the loader for the rules version (`runs`), and
what a registered runner drives (`masking`: `clean`, the locales and `mask_pii`, M7 PR 3); a
guard's runner is registered by the PR that builds it (`harness.register`).
"""
