"""M7's guards (ADR 0018): a pipeline that only tightens, around services that stay unchanged.

Everything here is pure, except reading the configuration folder at startup (`config`) and
writing the one metadata-only `guard` record per request (`types.emit_guard_record`):
- `types`: the closed codes, `GuardDecision` and the `guard` record (G10);
- `normalize`: `clean` for the model, `fold` for the screens, the invisible-character rule and
  the uncovered-script check (G5, D3 (i));
- `caps`: the input cap per surface that bounds every synchronous check (G3);
- `config`: the folder's loader and each business's resolved terms and guidance (G4);
- `match`: H1, `match_terms`, shesha's `[HAND-WRITE]` (D5 (b));
- `selftest`: every configured term must match itself and its obfuscated variants (G14);
- `checksums`: Verhoeff and Luhn over Unicode digits; they pick a mask's label, never whether
  a span is masked (G6);
- `locales`: the PII patterns per locale as data, G3's pattern rule, the ASCII view (G6);
- `pii`: `Masked`, the placeholders and H2, `mask_pii` (`[HAND-WRITE]`, D5 (b): written by
  Claude at shesha's request);
- `redact`: the API's log redaction (G10): `RedactingJsonFormatter`, the uvicorn.error rewrite,
  the access line's route template, installed by `create_app` only.

The routes call the guards explicitly (G13): no screening middleware, no screening dependency
(`redact.RequestScope` only lets the access line name the route). Nothing in
`evalrun/`, `intenteval/`, `tooleval/`, `extracteval/`, `agent/service.py`, `agent/loop.py`,
`intents/` or `extraction/` (but its route) imports this package.
"""
