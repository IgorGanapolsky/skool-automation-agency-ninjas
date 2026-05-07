# Skool RAG Pipeline

This repo has a local, dependency-free RAG-style index for captured Skool snapshots.

It does not scrape Skool, bypass private content, or claim collapsed content was ingested. It only indexes Markdown/text snapshots saved in `data/raw/skool_snapshots`.

## Commands

```bash
scripts/skool_rag.py ingest
scripts/skool_rag.py ask "ManyChat retainer Meta comment keyword funnel" --top-k 5
scripts/skool_rag.py signals --top-k 5
```

## Outputs

- `data/processed/skool_rag/documents.jsonl`
- `data/processed/skool_rag/tfidf_index.json`
- `data/processed/skool_rag/buyer_signals.jsonl`

## Current Top Signal

The 2026-05-07 visible ManyChat/Meta DM post ranks highest because it contains a concrete buyer pattern:

- Existing comment-to-DM funnel.
- High lead/call volume.
- Clear operational risk.
- Natural $499 diagnostic or retainer wedge.

Confidence is `medium_visible_excerpt` because only the visible excerpt was captured.
