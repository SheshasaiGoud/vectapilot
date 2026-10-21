# Ingestion fixtures — synthetic

Every file here is **synthetic**: invented text for fictional businesses, written for tests. No real
customer, business or personal data. The `*.golden.json` files are the expected loader and chunker
output; regenerate them with `UPDATE_GOLDEN=1 uv run pytest tests/ingestion` and review the diff by
hand. `policy.pdf` is written by `tests/ingestion/pdf_builder.py` (the same command rebuilds it,
and a test checks the committed bytes match).
