# data/widget — the customer widget per business (M8 PR 1)

> **Synthetic and agent-drafted.** The businesses are the fictional ones of
> [`data/sample/`](../sample/README.md); every phone number, address and e-mail is invented. The
> Hindi and Telugu texts were drafted by Claude and are **not reviewed by a native speaker** (D9):
> each file's `review` says so until a named reviewer signs off. Not for a real business until
> Q4c is answered.

The widget's configuration folder (`VECTAPILOT_WIDGET_CONFIG_DIR`; loader:
`apps/api/src/vectapilot_api/widget/config.py`; ADR 0019, rule 9). `infra/compose.yaml` mounts it
read-only. Optional: without the setting no business has a widget, and a business without a file
has none either (the config route and session creation answer 404).

```
data/widget/
  README.md                      this file (ignored by the loader)
  businesses/<business_id>.json  one business's widget
```

**What a file holds:** `enabled`, `display_name`, `allowed_origins` (the sites that may frame the
widget, written into the page's `frame-ancestors` from PR 2), `languages` (each one its corpus
manifest supports; the first is the default) and per language its `texts`: `greeting`,
`contact_fallback` lines, optional `quick_replies` (sent as the customer's own message), and the
optional follow-up texts `handoff_follow_up` and `urgent_follow_up` (D11 (b)). Also
`handoff_retention_days` (1–90, P5) and an optional `accent_color`.

**What it never holds:** the emergency guidance. It comes from the guard folder
(`data/safety/businesses/<business_id>.json`, S8), English only, and the config route sends it
verbatim with `guidance_language: "en"`.

**The follow-up texts are left out on purpose.** What they say for the two sample businesses is
shesha's to write with Q9 (the plan's open question 1); absent means the widget shows nothing
there, never a generic "a person will reply".

**The API refuses to start** on any of these (each has a test, `tests/widget/test_config.py`):
- an origin that is not exactly `scheme://host[:port]` in lowercase: a path, query, fragment,
  user, wildcard, whitespace, `;` or `,`, a default port, an IP address other than `127.0.0.1`,
  IPv6, or `http` (allowed only for `localhost` and `127.0.0.1`, and only when
  `VECTAPILOT_ENVIRONMENT` is `local`); an enabled widget with no origin;
- an enabled widget of a health business (`human_healthcare`, `veterinary`) whose guard file has
  no guidance (S8: it would have nothing to show first);
- a UI language its corpus manifest does not support, or `texts` that do not have exactly one
  entry per language;
- `accent_color` below 4.5:1 contrast against the white text written on it (WCAG 2.2 SC 1.4.3)
  [proposal; sofia confirms the text colour in PR 4];
- `synthetic` not `true`, an unknown key, a file named after another business, or any field out
  of its bounds.

Errors name the file and the location, never a value.
