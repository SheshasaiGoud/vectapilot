"""M7's guards (ADR 0018): a pipeline that only tightens, around services that stay unchanged.

Everything here is pure, except reading the configuration folder at startup (`config`) and
writing the one metadata-only `guard` record per request (`types.emit_guard_record`):
- `types`: the closed codes, `GuardDecision` and the `guard` record (G10);
- `normalize`: `clean` for the model, `fold` for the screens, the invisible-character rule and
  the uncovered-script check (G5, D3 (i));
- `caps`: the input cap per surface that bounds every synchronous check (G3);
- `config`: the folder's loader and each business's resolved terms and guidance (G4);
- `match`: H1, `match_terms`, shesha's `[HAND-WRITE]` (D5 (b));
- `selftest`: every configured term must match itself and its obfuscated variants (G14).

The routes call the guards explicitly (G13): no middleware, no screening dependency. Nothing in
`evalrun/`, `intenteval/`, `tooleval/`, `extracteval/`, `agent/service.py`, `agent/loop.py`,
`intents/` or `extraction/` (but its route) imports this package.
"""
