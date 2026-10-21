"""Ingestion (M2): uploaded files to typed documents to chunks with citation metadata.

Pure and business-agnostic: no network, no database, no embeddings here (PR 4 embeds, PR 5 writes
the rows under `business_id` and row-level security).
"""
