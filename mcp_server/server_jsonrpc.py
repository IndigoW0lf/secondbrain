"""
mcp_server/server_jsonrpc.py

JSON-RPC 2.0 compliant MCP server for Claude desktop.
Claude desktop spawns mcp_stdio.py which proxies here.

Run with:
  python mcp_server/server_jsonrpc.py
  (or via uvicorn: uvicorn mcp_server.server_jsonrpc:app --port 8000)
"""

import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from storage.store import get_db, semantic_search

app = FastAPI(title="SecondBrain MCP Server")

# ------------------------------------------------------------------ #
# Tool definitions                                                     #
# ------------------------------------------------------------------ #

TOOLS = [
    {
        "name": "search_transactions",
        "description": (
            "Search financial transactions by merchant, category, date range, or amount. "
            "Use for questions like 'what did I spend on groceries last month', "
            "'show me all Amazon charges', 'how much did I spend on dining in March'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Merchant name or description to search"},
                "category": {"type": "string", "description": "Spending category"},
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                "min_amount": {"type": "number", "description": "Minimum transaction amount"},
                "max_amount": {"type": "number", "description": "Maximum transaction amount"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            }
        }
    },
    {
        "name": "get_spending_summary",
        "description": (
            "Get spending grouped by category or merchant for a time period. "
            "Use for 'how much did I spend this month', 'biggest spending categories'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                "group_by": {
                    "type": "string",
                    "enum": ["category", "merchant", "account"],
                    "description": "How to group results"
                },
            },
            "required": ["start_date", "end_date"]
        }
    },
    {
        "name": "get_account_balances",
        "description": "Get current balances for all linked bank and credit card accounts.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "search_emails",
        "description": (
            "Search Gmail emails semantically. "
            "Use for 'find emails about my car insurance', 'find that receipt from Home Depot'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "from_address": {"type": "string", "description": "Filter by sender email"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_all",
        "description": (
            "Search across ALL data — emails, AI chats, documents, bookmarks. "
            "Use when you don't know where something is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "integer", "description": "Max results per source (default 5)"},
            },
            "required": ["query"]
        }
    },
]

# ------------------------------------------------------------------ #
# Tool implementations                                                 #
# ------------------------------------------------------------------ #

def search_transactions(query=None, category=None, start_date=None,
                        end_date=None, min_amount=None, max_amount=None, limit=20):
    conditions, params = [], []
    if query:
        conditions.append("(LOWER(description) LIKE ? OR LOWER(merchant_name) LIKE ?)")
        params.extend([f"%{query.lower()}%", f"%{query.lower()}%"])
    if category:
        conditions.append("LOWER(category) LIKE ?")
        params.append(f"%{category.lower()}%")
    if start_date:
        conditions.append("date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("date <= ?")
        params.append(end_date)
    if min_amount is not None:
        conditions.append("amount >= ?")
        params.append(min_amount)
    if max_amount is not None:
        conditions.append("amount <= ?")
        params.append(max_amount)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT t.date, t.amount, t.merchant_name, t.description,
               t.category, t.subcategory, t.pending, a.institution, a.name as account_name
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        {where}
        ORDER BY t.date DESC LIMIT ?
    """
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_spending_summary(start_date, end_date, group_by="category"):
    group_col = {
        "category": "COALESCE(t.category, 'Uncategorized')",
        "merchant": "COALESCE(t.merchant_name, t.description)",
        "account": "a.name",
    }.get(group_by, "COALESCE(t.category, 'Uncategorized')")
    sql = f"""
        SELECT {group_col} as group_name,
               COUNT(*) as transaction_count,
               ROUND(SUM(t.amount), 2) as total_spent,
               ROUND(AVG(t.amount), 2) as avg_transaction
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.date BETWEEN ? AND ?
          AND t.pending = 0 AND t.amount > 0
        GROUP BY {group_col}
        ORDER BY total_spent DESC LIMIT 30
    """
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, (start_date, end_date)).fetchall()]


def get_account_balances():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT institution, name, type, subtype, mask,
                   current_balance, available_balance, last_synced
            FROM accounts ORDER BY institution, type, name
        """).fetchall()]


def search_emails_tool(query, from_address=None, limit=10):
    try:
        results = semantic_search("emails", query, n_results=limit)
    except Exception:
        results = []
    with get_db() as conn:
        sql_params = [f"%{query}%", f"%{query}%"]
        sql = "SELECT id, from_address, from_name, subject, date, snippet FROM emails WHERE (subject LIKE ? OR snippet LIKE ?)"
        if from_address:
            sql += " AND from_address LIKE ?"
            sql_params.append(f"%{from_address}%")
        sql += f" ORDER BY date DESC LIMIT {limit}"
        keyword_rows = [dict(r) for r in conn.execute(sql, sql_params).fetchall()]
    seen, merged = set(), []
    for r in results:
        if r["id"] not in seen:
            seen.add(r["id"])
            merged.append({"id": r["id"], "match_type": "semantic", **r["metadata"]})
    for r in keyword_rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            r["match_type"] = "keyword"
            merged.append(r)
    return merged[:limit]


def search_all(query, limit=5):
    results = {}
    for collection in ["emails", "ai_conversations", "documents", "bookmarks"]:
        try:
            hits = semantic_search(collection, query, n_results=limit)
            if hits:
                results[collection] = hits
        except Exception:
            pass
    return results


# ------------------------------------------------------------------ #
# JSON-RPC 2.0 dispatch                                               #
# ------------------------------------------------------------------ #

def dispatch(req: dict) -> dict | None:
    method = req.get("method", "")
    params = req.get("params", {})
    req_id = req.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    # Lifecycle
    if method == "initialize":
        return ok({
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "secondbrain", "version": "1.0.0"}
        })

    if method == "notifications/initialized":
        return None  # notification — no response needed

    if method == "ping":
        return ok({})

    # Tool listing
    if method == "tools/list":
        return ok({"tools": TOOLS})

    # Tool calling
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        try:
            if name == "search_transactions":
                result = search_transactions(**args)
            elif name == "get_spending_summary":
                result = get_spending_summary(**args)
            elif name == "get_account_balances":
                result = get_account_balances()
            elif name == "search_emails":
                result = search_emails_tool(**args)
            elif name == "search_all":
                result = search_all(**args)
            else:
                return err(-32601, f"Unknown tool: {name}")

            return ok({
                "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
            })
        except Exception as e:
            return err(-32000, str(e))

    return err(-32601, f"Unknown method: {method}")


# ------------------------------------------------------------------ #
# HTTP endpoint (used by mcp_stdio.py proxy)                          #
# ------------------------------------------------------------------ #

@app.post("/mcp")
async def mcp_http(request: Request):
    body = await request.json()
    response = dispatch(body)
    if response is None:
        return JSONResponse({})
    return JSONResponse(response)


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MCP_PORT", 8000))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    print(f"Starting MCP server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)