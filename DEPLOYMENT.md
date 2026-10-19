# Streamlit Community Cloud deployment

This project has two entry points:

| Entry point | Intended use | Finance data |
|---|---|---|
| `streamlit_app.py` | Public demo | Fictional samples in a temporary database per browser session |
| `app.py` | Personal desktop | Persistent local SQLite database |

## Deploy the public demo

1. Sign in at [Streamlit Community Cloud](https://share.streamlit.io/) using the
   GitHub account that can access `Moobakr/Personal-Finance-Agent`.
2. Select **Create app** and deploy an existing GitHub app.
3. Use repository `Moobakr/Personal-Finance-Agent`, branch `main`, and main file
   `streamlit_app.py`.
4. Open **Advanced settings** and select **Python 3.12**. Community Cloud installs
   `requirements.txt`; optional semantic/stock requirements are not needed.
5. In **Secrets**, enter the settings below privately, replacing the placeholder
   with the OpenRouter key. Never commit the populated secrets file.

```toml
OPENROUTER_API_KEY = "YOUR_OPENROUTER_KEY"
OPENROUTER_MODEL = "openrouter/free"
DEMO_SHARED_AI = true
```

6. Choose an available `streamlit.app` subdomain and deploy. Verify Overview,
   Assistant, Transactions, Goals and Tools after the build succeeds.
7. Open the app in two separate browser sessions and confirm that changing one
   session's data does not change the other. Try an AI request and a stock lookup.
8. Add the verified live URL to the README and the GitHub repository's Website field.

The Streamlit account's GitHub OAuth authorization must be completed by the account
owner. If prompted for new repository permissions or terms, review them before
continuing. A deployment URL is only considered live after the app has loaded and
its essential flows have been checked.

Official references: [deployment](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy),
[secret management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management).

## Public demo behavior

- The app seeds ten fictional transactions and two goals in each new session.
  Sample dates use the current month. No personal JSON/database is read or migrated.
- Changes belong to that browser session; refreshing the connection or restarting
  the server can reset them. Export CSV to keep edits. **Reset demo data** restores
  the sample workspace and clears chat/tool results.
- Shared AI uses the server key only when `DEMO_SHARED_AI` is explicitly true.
  Otherwise a visitor can provide a key for that session, or use local tools.
- Each session has five AI attempts with a 15-second cooldown. Shared-key attempts
  are also capped at twenty per rolling 24 hours in the running app. Failed attempts
  count. These in-memory limits reset on server restart, so keep provider-side
  limits configured on the key. The default router uses free models only.
- Questions, aggregate results and recent conversation are sent to OpenRouter for
  advice. Use fictional data in the public demo. No key is put into prompts,
  exports or application logs.
- Only request counters are cached across visitors. Databases, chat histories,
  optional visitor keys and retrieval indexes belong to the individual session.

## Release verification

Run tests and dependency checks before pushing:

```powershell
python test.py
python -m pip check
python scripts/release.py --output path/to/personal-finance-agent-release.zip
```

The packaging script includes only explicitly allowed source, documentation,
reference documents and fictional samples. It excludes local secrets, real finance
records, SQLite databases, virtual environments and generated vector stores.
GitHub Actions checks the same baseline on Linux after pushes and pull requests.

Update the existing repository with normal commits. Preserve its commit history;
do not force-push or delete/recreate it just to replace the app implementation.
