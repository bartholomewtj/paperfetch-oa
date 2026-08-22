---
name: paperfetch-oa
description: >
  Fetch legal open-access full text for a DOI or title with `papers get`.
  Use when the user or agent needs a paper's full text or a DOI lookup.
---

# paperfetch-oa

Need `PAPERS_MAILTO` set to the user's real email.

## Get

```
papers get <doi-or-title>
```

Stdout is one JSON object. Do not parse anything else.

## If `status` is `ok`

1. Read the file at `read`
2. Use at most `max_chars` characters
3. Cite the DOI and `version`
4. Do not attach the PDF

## If `status` is `no_oa`, `no_doi`, or any other status

Use the abstract only. Do not keep fetching.

## Status

```
papers status
```

Local JSON summary of cache counts. Read-only, not a fetch.

## Never

- Do not use another downloader
- Do not fetch paywalled full text
