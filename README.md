# Personal Finance Agent

A Python and Streamlit finance assistant with spending dashboards, transaction
management, savings goals, currency conversion, stock prices and optional
OpenRouter advice grounded in calculated totals and a local knowledge base.

## Try it

**[Open the live demo](https://moobakr-personal-finance-agent.streamlit.app/)**

Hosted free on Streamlit Community Cloud. Shared AI advice is enabled with the
request limits described below. You can also run the same demo locally:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Use **Python 3.12**. On Linux/macOS, use `.venv/bin/python` instead of the Windows
Python path. Open the localhost URL printed by Streamlit.

The demo starts with fictional sample transactions and goals. Each browser session
gets a separate temporary SQLite workspace. Edits and imports do not affect other
visitors; session/server restarts may reset them. Export CSV to keep your changes.
**Reset demo data** restores the samples. Use fictional data in the public demo.

## Features

- **Overview:** income, expenses, net balance, savings rate, monthly charts and
  category breakdowns. Totals remain separate for every currency.
- **Assistant:** questions about summaries, categories and goals, direct currency
  conversion and stock lookup, and optional contextual AI advice with source files.
- **Transactions:** add, edit, confirm deletion, literal text search, 25-row paging,
  CSV preview/import/export and duplicate handling.
- **Goals:** manage multiple savings goals independently and track progress.
- **Tools:** currency conversion with a labeled offline fallback and stock daily
  closes with quote currency, source, market date and retrieval time.

Reports include all matching records, including those outside the current page.
Month and currency filters apply to dashboard totals, transactions and chat defaults.

Example questions:

- `Summarize my spending and suggest a plan`
- `Summary this month in EGP`
- `Categorize my expenses`
- `Show goal progress`
- `Convert 100 USD to EGP`, followed by `And 50?`
- `Stock price AAPL`, followed by `What about MSFT?`

Transaction reports accept one `YYYY-MM`, `this month`, `last month`, or `all dates`.
Named-month ranges, selected-month comparisons and unrestricted multi-step tool
planning are not implemented. Chat changes no saved records; use the forms to edit.

## Personal mode

For persistent local use, run:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Personal mode uses `data/finance.sqlite3`. Existing transaction/goal JSON files are
validated and migrated once in a single database transaction. Original files remain
unchanged. Fresh clones start empty; add records or import the fictional
[example CSV](examples/sample_transactions.csv).

To back up personal data, stop the app and copy the SQLite database. Personal
records, credentials, virtual environments and generated indexes are excluded from
Git and the release ZIP. Relative paths resolve from the project folder.

## AI configuration

Local dashboards and tools work without an AI key. To enable advice, copy
`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and enter your
OpenRouter key privately:

```toml
OPENROUTER_API_KEY = "YOUR_OPENROUTER_KEY"
OPENROUTER_MODEL = "openrouter/free"
```

Environment variables override local settings. Never commit the populated secrets
file. The model is configurable; provider availability and request limits may vary.

For the public demo, shared-key use requires `DEMO_SHARED_AI = true`. Enter the key
in Community Cloud's Secrets dashboard, using the cloud secrets example as a guide.
A visitor can alternatively enter their own key in **Demo AI** for that session.
Shared AI is limited to 20 attempts per rolling 24 hours in the running app; each
session has five attempts with a 15-second cooldown. These in-memory limits reset
when the server restarts; provider-side limits still apply. Local personal mode has
no demo request limits.

Advice sends the question, calculated totals, relevant goal/category results,
retrieved knowledge and up to four recent conversation turns to OpenRouter.
Transaction rows and merchant names are not included automatically. Anything typed
into the question or knowledge documents may be included. Keys are used for
provider authentication and are not included in model prompts or finance exports.

## External data

Currency conversion uses open.er-api.com and clearly labels saved fallback rates
as possibly outdated. Conversion does not rewrite records or combine currencies.
Stock prices use Yahoo Finance daily chart data through the baseline requests
package: no stock API key or yfinance installation is needed. During an open
session, the previous completed daily close is shown. `GBp` means pence, not pounds.
Live tools need an internet connection and are subject to provider availability.

Knowledge search defaults to offline word-based embeddings. Optional semantic
search can be installed through `requirements-semantic.txt`; set
`EMBEDDING_BACKEND = "sentence_transformers"` and allow the first model download
with `ALLOW_MODEL_DOWNLOAD = "true"`. The app falls back to local search if that
model cannot load or encode. Public demo sessions always use the offline backend.

## CSV and storage

Required CSV columns are `date`, `amount`, `currency`; optional columns are `id`,
`merchant`, `description`, `category`. Use UTF-8, YYYY-MM-DD dates, decimal points,
negative expenses and positive income. Import supports 2 MB / 10,000 records.

The preview reports duplicates before import. Matching date, amount, currency,
merchant and description are skipped by default. Uncheck **Skip matching
transactions** for genuinely separate identical purchases. Conflicting IDs or any
invalid row reject the entire import. Filtered exports include all search matches
across pages. **Export all transactions** exports the whole current workspace.

SQLite stores money as integer cents. Calculations use decimal arithmetic; currency
codes have three letters and amounts have at most two decimal places. Optional
path settings include `DATABASE_PATH`, `TRANSACTIONS_PATH`, `GOALS_PATH`,
`FX_FALLBACK_PATH`, `KNOWLEDGE_DIR` and `RAG_STORE_DIR`. Public demo mode overrides
private database, migration and index paths with session-owned temporary paths.

## Verification

```powershell
.\.venv\Scripts\python.exe test.py
.\.venv\Scripts\python.exe -m pip check
```

Tests use temporary data and mock external services. They cover calculations,
mixed currencies, input validation, SQLite migration/rollback, imports, retrieval,
provider failures, stock trading-session selection and Streamlit interactions.
See [VALIDATION.md](VALIDATION.md) for live checks and remaining limitations.
`requirements-lock.txt` records the verified Windows environment; Community Cloud
and CI install the cross-platform direct pins in `requirements.txt`.

## Deploy

See [DEPLOYMENT.md](DEPLOYMENT.md) for Streamlit Community Cloud instructions.
Use repository `Moobakr/Personal-Finance-Agent`, branch `main`, entry point
`streamlit_app.py`, and Python 3.12. `app.py` is the persistent personal entry point.

## Structure

```text
app.py                  Personal desktop entry point
streamlit_app.py        Public session-isolated demo entry point
finance_agent/          Configuration, storage, calculations, retrieval, tools and UI
knowledge/              Finance reference documents
examples/               Fictional import samples
tests/                  Unit, integration and Streamlit regression tests
scripts/                Safe release packaging
```
