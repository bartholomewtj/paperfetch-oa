# paperfetch-oa

Get a paper's **open-access** full text from a DOI or title.

Resolver ladder: cache → Europe PMC → US PMC → bioRxiv / medRxiv API → Unpaywall → OpenAlex → Semantic Scholar → preprint shortcuts → CORE (with key). US PMC also reads the article page itself when PMC has no downloadable PDF — that is how NIH author manuscripts (in PMC, but not in its open-access subset) come back as text. For `10.1101/` preprints the bioRxiv / medRxiv step asks Cold Spring Harbor's own API (`api.biorxiv.org`) which server holds the paper and which version is newest, then fetches that version's PDF; `meta.json` records `resolver: biorxiv`, `version: vN` and `server` (`biorxiv` or `medrxiv`). The guessed `v1` URL in the preprint shortcuts stays as the last resort.

This is the public subset of a private tool. It only fetches copies that are already open. It does not log in anywhere, does not keep a library pickup list, and does not ingest files you downloaded by hand.

Do not install this next to the private `paperfetch` package. Both expose the `papers` command and the `papers` Python package. This one is for a public host (or a machine that only needs OA).

## Install

```
pip install git+https://github.com/bartholomewtj/paperfetch-oa.git
```

Or from a clone:

```
pip install -e .
```

Set `PAPERS_MAILTO` to your real email. Unpaywall, OpenAlex, and Crossref require it. Do not use a made-up address.

```
$env:PAPERS_MAILTO="you@your-domain"
```

Optional: set `SEMANTIC_SCHOLAR_API_KEY` for higher Semantic Scholar rate limits (keyless traffic 429s and is then skipped for the rest of the process; never commit keys).

Optional: set `CORE_API_KEY` to add [CORE](https://core.ac.uk/services/api) as the last resolver. Register free at https://core.ac.uk/api-keys/register. Without a key CORE is skipped.

## Commands

```
papers get 10.1371/journal.pone.0000308
papers get "Sharing detailed research data is associated with increased citation rate"
papers get 10.1371/journal.pone.0000308 10.1001/jamapsychiatry.2018.1776
papers get - < dois.txt
papers status
```

If `papers` is not on PATH, use `python -m papers`.

`papers get` prints one JSON object on stdout. When given a title, it prints `resolved title -> {doi}` on stderr before running the ladder. Unknown title returns `{status: "no_doi", agent_next: "notify_human"}` and exits 1. Agents read `text.txt` (path in `read`), not the PDF.

### What `text.txt` looks like

Each cached paper's `text.txt` has a marker line before every standard section it found, then a blank line, then the section's text:

```
## Introduction

Sharing information facilitates science. ...

## Results

Of the 85 publications, ...
```

The standard sections are `abstract`, `introduction`, `methods`, `results`, `discussion` and `conclusions`. Title, authors and anything before the first heading stay at the top with no marker. Subsections and other headings that are not recognised ("Genotyping", "Patient characteristics") stay in the body as plain lines under the nearest marker. The reference list, acknowledgements, funding, supporting-information lists and repeated page headers and footers are dropped, so a volume or page number in a citation cannot be mistaken for a result. This is the same shape on every route: PDF, Europe PMC XML and PMC HTML.

`meta.json` records what was found as `"sections": ["introduction", "results", "discussion", "methods"]` (lowercase, in document order). A PDF with no detectable headings still extracts as plain text, with no markers and `"sections": []`.

PDFs are read with PyMuPDF (it replaced pypdf). It keeps reading order in two-column layouts and exposes font sizes, which is how headings are found: a line that names a standard section and is larger than the body text, bold, or in capitals.

`papers get` takes more than one DOI or title, and `papers get -` reads one per line from stdin (blank lines skipped). Output is one JSON line per input, in input order, each the same record a single `get` prints. Exit code is 0 when every line is `ok`, else 2. Use this when fetching a list: one process means the Semantic Scholar rate-limit skip and the Unpaywall error memo hold across the whole batch, so a keyless run pays the 30-second 429 sleep once, not once per DOI. A single DOI behaves exactly as before.

A usage error (for example `PAPERS_MAILTO` unset) prints `{"status": "config_error", "reason": "...", "agent_next": "notify_human; stop_fetch"}` on stdout and exits 1, so stdout is always JSON.

Statuses:

| Status | Meaning |
|---|---|
| `ok` | Full text on disk. Read the file at `read`. |
| `no_oa` | No open-access copy found. `tried` lists the resolvers. `unpaywall_blocked` means the publisher PDF refused a script. |
| `unreadable_pdf` | Got a PDF, no extractable text (likely a scan). |
| `retry` | Unpaywall API unreachable and nothing else hit. Try later. |
| `no_doi` | Title did not resolve to a DOI. |
| `config_error` | Bad setup, such as no `PAPERS_MAILTO`. `reason` says what to fix. |

Unpaywall tries every open location it knows, repository copies (PMC etc.) before publisher sites. A failed download or a PDF with no extractable text moves on to the next location, then the next resolver, rather than stopping.

Title lookup skips Crossref `posted-content` (preprints, Nature Precedings) and reviewer reports, and prefers a `journal-article`. Give a DOI when you have one — titles are fuzzy.

`papers status` prints one JSON object, exits 0, touches no network:

- `cached`: count of cached papers (`text.txt` ≥ 500 chars) and total `chars`
- `unreadable`: count of paper dirs with PDF but text under the floor
- `cache_root`: cache directory
- `mailto_set` / `s2_key_set` / `core_key_set`

Cache lives in `%USERPROFILE%\.paperfetch` (or `~/.paperfetch`).

## Tests

```
python -m pytest -q
```

Offline only. No network, no keys.
