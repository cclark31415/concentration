# concentration

The classic memory-matching card game, served at [concentration.chrisclark.net](https://concentration.chrisclark.net).

A tiny Flask app that renders a single HTML page. All game logic runs client-side.

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://localhost:5000>. You can also just open [templates/index.html](templates/index.html) directly in a browser — the game is 100% client-side.

## Layout

```
app.py                          # Flask: serves templates/index.html at /
Procfile                        # gunicorn app:app (used by Azure App Service)
requirements.txt                # Flask, Werkzeug, gunicorn
templates/index.html            # the game (HTML + CSS + JS, no build step)
.github/workflows/deploy.yml    # CI: build + deploy on push to main
```

## Deployment

Pushes to `main` are built and deployed automatically via GitHub Actions
([.github/workflows/deploy.yml](.github/workflows/deploy.yml)). The app runs on
Azure App Service with user data stored in Azure Table Storage; auth to Azure
uses OIDC federated credentials (no long-lived secrets in the repo).

> Infrastructure setup, Azure resource details, and the privacy / data-request
> runbook are kept in a private repository.

## Storage connection in code

The app uses `DefaultAzureCredential`, which picks the right auth automatically:
- **In Azure**: the App Service's system-assigned managed identity.
- **Locally**: your `az login` credentials.
- **Azurite**: auth via the well-known dev connection string (see below).

```python
import os
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential

if os.environ.get("AZURE_STORAGE_USE_AZURITE") == "true":
    svc = TableServiceClient.from_connection_string(
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
    )
else:
    svc = TableServiceClient(
        endpoint=os.environ["AZURE_STORAGE_TABLE_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )
```

> The Azurite `AccountKey` above is Microsoft's public, well-known emulator
> development key — it is not a secret.

### Table schemas

**`users`** — one row per person
- `PartitionKey` = first 2 chars of `sha1(email)`
- `RowKey` = normalized email (lowercased, trimmed)
- `display_name` (string)
- `providers` (JSON string, e.g. `[{"provider":"google","subject":"1098..."}]`)
- `created_at`, `last_login` (ISO-8601 strings)

**`games`** — one row per completed game
- `PartitionKey` = normalized email
- `RowKey` = `f"{(2**63-1) - epoch_ms:020d}_{ulid}"` (newest first on range scans)
- `level`, `pairs`, `moves`, `duration_ms`, `completed_at`, `client_version`

## Local development with Azurite

[Azurite](https://github.com/Azure/Azurite) is Microsoft's in-process emulator for Azure Storage. It stores everything in a local folder and responds on `127.0.0.1`.

```bash
# One-time install (Node 18+ required)
npm install -g azurite

# Run just the Table service — drop data into ~/.azurite
azurite-table --location ~/.azurite --silent --tableHost 127.0.0.1 --tablePort 10002
```

Run the Flask app against it:

```bash
export AZURE_STORAGE_USE_AZURITE=true
python app.py
```

The first time the app boots against Azurite, it creates the `users` and `games` tables. Everything persists in `~/.azurite` between runs — blow it away with `rm -rf ~/.azurite` to reset.

## Environment variables (reference)

| Variable | Where | Purpose |
| --- | --- | --- |
| `AZURE_STORAGE_TABLE_ENDPOINT` | App Service | Table endpoint URL (prod only) |
| `AZURE_STORAGE_USE_AZURITE` | local dev `.env` | When `true`, use Azurite connection string instead of MSI |
| `FLASK_SECRET_KEY` | App Service + `.env` | Session cookie signing (set when auth ships) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | App Service + `.env` | Google OAuth (set when auth ships) |
| `MS_CLIENT_ID` / `MS_CLIENT_SECRET` | App Service + `.env` | Microsoft OAuth (set when auth ships) |
