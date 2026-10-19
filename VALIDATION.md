# Validation

Release verification performed on 19 October 2026 with Python 3.12.

| Check | Result |
|---|---|
| Regression suite | 121 tests passed |
| Cross-currency totals and full-selection reports | Passed; decimal calculations and separate currency totals |
| SQLite migration, rollback, restart, concurrent writes | Passed with temporary data |
| CSV validation, duplicates, atomic imports and export round trips | Passed |
| Actual Streamlit interactions | Dashboard, transaction/goal CRUD, CSV, chat and tools passed |
| Public demo isolation | Separate browser sessions retain separate transactions and goals; private source paths are ignored |
| Public demo reset | Restores fictional samples and clears chat/tool snapshots; preserves request counters |
| Shared AI controls | Explicit shared-key opt-in, five attempts/session, cooldown and thread-safe global rolling limit verified |
| Credential boundaries | Key excluded from prompts and finance records; populated secrets and private databases excluded from release |
| Live OpenRouter | Free-model router returned a response; sample-data advice through the public entry point passed with source references |
| Live FX | USD-to-EGP request returned a live API rate; fallback and failures also tested with controlled responses |
| Live stocks | AAPL and MSFT succeeded; actual Fetch button passed without yfinance; partial daily bars excluded while market open |
| Retrieval | Index validation, rebuilds, changed corpus, missing semantic model, process stability and memory-cache invalidation passed |
| Search timing | 20 documents, 30 warm queries: median 9.454 ms before / 5.494 ms after; same top source IDs |
| Personal data preservation | Local database unchanged from the pre-release backup; no demo operation touches personal records |

Tests mock external services except the explicitly described live smoke checks.
Those checks use public tickers/rates and fictional sample finance data, and never
print credentials. The old unavailable Xiaomi model and yfinance 0.2.54 lookup
were replaced with the configurable free-model router and a requests-based stock
client respectively.

A fresh Windows Python 3.12 virtual environment installed the baseline requirements,
passed pip check and passed all 121 tests. The same 121-test suite and pip check
also passed on Ubuntu with Python 3.12 in [GitHub Actions](https://github.com/Moobakr/Personal-Finance-Agent/actions/runs/39318742653).

The [public Streamlit deployment](https://moobakr-personal-finance-agent.streamlit.app/)
loaded successfully, and an unauthenticated HTTP request returned 200 without a
login redirect. Shared-key AI returned advice from the deployed app. In two
independent browser sessions, adding a fictional transaction increased one
workspace to 11 records while the other remained at 10 after refresh. Reset
restored the first workspace to the original 10 records. The hosted currency
converter also returned a live USD-to-EGP rate, and its stock Fetch button
returned an AAPL completed daily close from Yahoo Finance.

## Boundaries

- Public demo data is temporary and session-isolated; it is not persistent account storage.
- Shared-AI counters are in process memory and reset when the server restarts.
  Provider-side key limits remain the durable quota control.
- Live providers can be unavailable or rate limited. Stock results are completed
  daily closes, not real-time prices; saved FX rates are explicitly labeled estimates.
- A real semantic transformer model has not been downloaded or benchmarked.
- Chat supports the documented single-tool requests and short follow-ups rather
  than unrestricted multi-step planning. Record changes use validated forms.
- The timing above is a local search microbenchmark, not an AI latency benchmark.
