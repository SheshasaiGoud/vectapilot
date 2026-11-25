"""The product pages the API serves on its own origin (M8 PR 2b; D2 (a) with (i), D3 (a), D12 (b),
S9, S13, ADR 0019 decision 4).

- `pages`: the widget page (`/widget/{business_id}`), the dashboard (`/staff/`) and the product
  build's static assets, read only from `ui_dir`, each with its security headers.
"""
