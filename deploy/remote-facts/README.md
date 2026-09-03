# Matchline remote facts collector

This directory is a small, dependency-free collector for the separate VPS
`ubuntu@170.106.198.250`. It is deliberately not part of the legacy
`wc-predict` service.

The collector fetches bounded, display-only facts from OpenLigaDB and Wikidata,
optionally MET Norway forecasts when coordinates are explicitly configured,
and records policy/transport diagnostics for ESPN and SofaScore. It writes one
atomically replaced JSON file and contains no prediction, odds, model, or
market values. Provider payloads are hashed for lineage but are not copied into
the cache.

Run once:

```bash
python3 /home/ubuntu/matchline-facts/collector.py \
  --output /home/ubuntu/matchline-facts/current.json
```

`MATCHLINE_MET_COORDINATES` may contain a small JSON object such as
`{"Berlin":[52.52,13.405]}`. Coordinates are display-only and must not be
treated as model features. The collector does not POST to Sites; an explicit
authenticated import step is required before the cache can become public.
