# Next session

_Last handoff: 22 August 2026 — branch `main`_

## Where this stopped

Public OA-only `papers` CLI. Ladder: cache → Europe PMC → US PMC → Unpaywall → OpenAlex → Semantic Scholar → preprints → CORE (with key). Missing OA is `no_oa`. PR #1 is merged: Crossref prefers the journal article over Nature Precedings, bioRxiv/medRxiv try each host once then retry, `europepmc` is in `tried`, Unpaywall scans fall through, OpenAlex walks every location PDF. 86 offline tests. Live: PLOS title → `10.1371/journal.pone.0000308`; medRxiv hit; JAMA `tried` starts with `europepmc`.

Private `paperfetch` is upstream. Shared ladder changes must land in both (see README). This package stays OA-only: no CKN, no `miss` / `ingest`.

## Resume with

```bash
cd C:/claudeOS/Projects/tools/paperfetch-oa
C:/Python314/python.exe -m pytest -q
```

Do **not** `pip install -e .` on this machine. Local `papers` is the private CLI.

## Next thing to do

1. In **article-generator** issue #173: accept `no_oa` in `articlegen/paperfetch.py` `NOT_OA_STATUSES`, then install this package in the Dockerfile from `https://github.com/bartholomewtj/paperfetch-oa.git`.
2. Do not change JSON field names here without updating article-generator.
3. Optional: add a CI workflow (`pytest` only, no network).

## Open

- article-generator #173 — hosted app still cannot fetch full text outside Europe PMC

## Watch out for

- Console script name `papers` collides with private paperfetch.
- Cache is still `~/.paperfetch` (same folder as private). Fine on Render.
- medRxiv is Cloudflare-flaky; the retry *order* is what we control.
- `PAPERS_MAILTO` required for a live `get`. Render can reuse `OPENALEX_MAILTO`.
