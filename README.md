# secondbrain

A self-hosted personal intelligence system. All your data, one AI interface.

## Architecture

```
ingestion/          # One pipeline per data source
  financial/        # Plaid → bank accounts + credit cards
  email/            # Gmail API
  calendar/         # Google Calendar API
  amazon/           # Order email parsing
  ai_chats/         # Claude, ChatGPT export ingestion
  files/            # Local files + Google Drive
  bookmarks/        # Browser bookmark exports
  notes/            # Apple Notes, Google Docs
storage/            # SQLite (structured) + ChromaDB (vectors)
mcp_server/         # Exposes your data to Claude as tools
scheduler/          # Cron-style job runner
utils/              # Shared helpers (embeddings, chunking, etc.)
config/             # Credentials and settings (never commit)
```

## Stack

- **Python 3.11+**
- **SQLite** — structured data (transactions, events, metadata)
- **ChromaDB** — vector embeddings (emails, notes, chats, docs)
- **Plaid** — financial data
- **Google APIs** — Gmail, Calendar, Drive
- **sentence-transformers** — local embeddings (no API cost)
- **FastAPI** — MCP server + optional web UI
- **APScheduler** — cron jobs

## Setup

```bash
# 1. Clone and create virtualenv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Copy config template and fill in credentials
cp config/secrets.example.env config/secrets.env

# 3. Initialize database
python storage/init_db.py

# 4. Run your first ingest
python ingestion/financial/plaid_ingest.py

# 5. Start MCP server
python mcp_server/server.py
```

## Connecting Claude

Once the MCP server is running on your VPS, add it to Claude's MCP config:

```json
{
  "mcpServers": {
    "secondbrain": {
      "url": "http://your-vps-ip:8000/mcp",
      "transport": "http"
    }
  }
}
```

Then in Claude: "What did I spend on groceries last month?"

## Data sources status

| Source | Status | Pipeline |
|---|---|---|
| Bank accounts | ✅ Ready | `ingestion/financial/plaid_ingest.py` |
| Credit cards | ✅ Ready | `ingestion/financial/plaid_ingest.py` |
| Gmail | 🔧 Next | `ingestion/email/gmail_ingest.py` |
| Google Calendar | 🔧 Next | `ingestion/calendar/gcal_ingest.py` |
| Amazon orders | 🔧 Next | `ingestion/amazon/amazon_ingest.py` |
| AI chat history | 🔧 Next | `ingestion/ai_chats/` |
| Local files | 🔧 Next | `ingestion/files/` |
| Bookmarks | 🔧 Next | `ingestion/bookmarks/` |
| Apple Notes | 🔧 Next | `ingestion/notes/` |
| Google Drive | 🔧 Next | `ingestion/files/gdrive_ingest.py` |
