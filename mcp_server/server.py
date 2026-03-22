"""
mcp_server/server.py

FastAPI-based MCP server. Exposes your personal data to Claude
as a set of callable tools.

Run with:
  uvicorn mcp_server.server:app --host 0.0.0.0 --port 8000

Then add to Claude's MCP config:
  { "url": "http://your-vps:8000/mcp", "transport": "http" }
"""

import json
import os
import sys
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

load_dotenv("config/secrets.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from storage.store import get_db, semantic_search

app = FastAPI(title="SecondBrain MCP Server")


class MCPToolCall(BaseModel):
    name: str
    arguments: dict = {}


class MCPRequest(BaseModel):
    method: str
    params: dict = {}


class MCPResponse(BaseModel):
    result: Any = None
    error: dict = None


TOOLS = [
    {
        "name": "search_transactions",
        "description": (
            "Search financial transactions by merchant, category, date range, or amount. "
            "Use this for questions like 'what did I spend on groceries last month', "
            "'show me all Amazon charges', 'how much did I spend on dining in March'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Merchant name or description to search"},
                "category": {"type": "string", "description": "Spending category (e.g. FOOD_AND_DRINK, TRAVEL, SHOPPING)"},
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
            "Get a spending summary grouped by category or merchant for a time period. "
            "Use for questions like 'how much did I spend this month', "
            "'what are my biggest spending categories', 'compare this month vs last month'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                "group_by": {"type": "string", "enum": ["category", "merchant", "account"], "description": "How to group results"},
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
            "Semantically search through Gmail emails. "
            "Use for questions like 'find emails about my car insurance', "
            "'what did the dentist office send me', 'find that receipt from Home Depot'."
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
        "name": "get_recent_emails",
        "description": "Get the most recent emails, optionally filtered by sender.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of emails (default 10)"},
                "from_address": {"type": "string", "description": "Filter by sender"},
            }
        }
    },
    {
        "name": "search_calendar_events",
        "description": (
            "Search Google Calendar events by date range and/or keyword. "
            "Combines SQLite filters with semantic search on the calendar collection when a keyword is provided."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD (optional)"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD (optional)"},
                "keyword": {"type": "string", "description": "Keyword for title, description, or location"},
                "limit": {"type": "integer", "description": "Max results (default 25)"},
            }
        }
    },
    {
        "name": "search_documents",
        "description": (
            "Semantic search across ingested documents and notes (Google Drive, Apple Notes, etc.). "
            "Use for questions like 'find my notes about taxes', 'which doc mentions the lease'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "source": {
                    "type": "string",
                    "description": "Optional filter: gdrive | apple_notes | local | bookmark",
                },
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_amazon_orders",
        "description": (
            "Look up Amazon orders parsed from email by order date or item text in the order line items."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Order date on/after YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "Order date on/before YYYY-MM-DD"},
                "item_query": {"type": "string", "description": "Substring to match in serialized line items"},
                "limit": {"type": "integer", "description": "Max rows (default 20)"},
            }
        }
    },
    {
        "name": "search_all",
        "description": (
            "Semantic search across ALL data sources — emails, AI chats, documents, bookmarks, calendar. "
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


def search_transactions(
    query: str = None,
    category: str = None,
    start_date: str = None,
    end_date: str = None,
    min_amount: float = None,
    max_amount: float = None,
    limit: int = 20
) -> list[dict]:
    conditions = []
    params = []

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
        ORDER BY t.date DESC
        LIMIT ?
    """
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_spending_summary(start_date: str, end_date: str, group_by: str = "category") -> list[dict]:
    group_col = {
        "category": "COALESCE(t.category, 'Uncategorized')",
        "merchant": "COALESCE(t.merchant_name, t.description)",
        "account": "a.name",
    }.get(group_by, "COALESCE(t.category, 'Uncategorized')")

    sql = f"""
        SELECT
            {group_col} as group_name,
            COUNT(*) as transaction_count,
            ROUND(SUM(t.amount), 2) as total_spent,
            ROUND(AVG(t.amount), 2) as avg_transaction
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.date BETWEEN ? AND ?
          AND t.pending = 0
          AND t.amount > 0
        GROUP BY {group_col}
        ORDER BY total_spent DESC
        LIMIT 30
    """
    with get_db() as conn:
        rows = conn.execute(sql, (start_date, end_date)).fetchall()
        return [dict(row) for row in rows]


def get_account_balances() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT institution, name, type, subtype, mask,
                   current_balance, available_balance, last_synced
            FROM accounts
            ORDER BY institution, type, name
        """).fetchall()
        return [dict(row) for row in rows]


def search_emails_tool(query: str, from_address: str = None, limit: int = 10) -> list[dict]:
    results = semantic_search("emails", query, n_results=limit)

    with get_db() as conn:
        sql_params = [f"%{query}%", f"%{query}%"]
        sql = """
            SELECT id, from_address, from_name, subject, date, snippet
            FROM emails
            WHERE (subject LIKE ? OR snippet LIKE ?)
        """
        if from_address:
            sql += " AND from_address LIKE ?"
            sql_params.append(f"%{from_address}%")
        sql += " ORDER BY date DESC LIMIT ?"
        sql_params.append(limit)
        keyword_rows = [dict(r) for r in conn.execute(sql, sql_params).fetchall()]

    seen = set()
    merged = []
    for r in results:
        if r["id"] not in seen:
            seen.add(r["id"])
            merged.append({"id": r["id"], "match_type": "semantic", **r["metadata"], "distance": r["distance"]})
    for r in keyword_rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            r["match_type"] = "keyword"
            merged.append(r)

    return merged[:limit]


def get_recent_emails(limit: int = 10, from_address: str = None) -> list[dict]:
    with get_db() as conn:
        if from_address:
            rows = conn.execute("""
                SELECT id, from_address, from_name, subject, date, snippet
                FROM emails WHERE from_address LIKE ?
                ORDER BY date DESC LIMIT ?
            """, (f"%{from_address}%", limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, from_address, from_name, subject, date, snippet
                FROM emails ORDER BY date DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def search_calendar_events(
    start_date: str = None,
    end_date: str = None,
    keyword: str = None,
    limit: int = 25,
) -> list[dict]:
    conditions = []
    params = []

    if start_date:
        conditions.append("date(start_dt) >= date(?)")
        params.append(start_date)
    if end_date:
        conditions.append("date(start_dt) <= date(?)")
        params.append(end_date)
    if keyword:
        kw = f"%{keyword.lower()}%"
        conditions.append(
            "(LOWER(COALESCE(title,'')) LIKE ? OR LOWER(COALESCE(description,'')) LIKE ? "
            "OR LOWER(COALESCE(location,'')) LIKE ?)"
        )
        params.extend([kw, kw, kw])

    where_sql = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT id, calendar_id, title, description, location, start_dt, end_dt, all_day, status
        FROM calendar_events
        WHERE {where_sql}
        ORDER BY start_dt ASC
        LIMIT ?
    """
    params.append(limit)

    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    seen = {r["id"] for r in rows}
    merged = list(rows)

    if keyword:
        try:
            sem = semantic_search("calendar", keyword, n_results=limit)
            for r in sem:
                eid = r["id"]
                if eid in seen:
                    continue
                seen.add(eid)
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT id, calendar_id, title, description, location, start_dt, end_dt, all_day, status "
                        "FROM calendar_events WHERE id=?",
                        (eid,),
                    ).fetchone()
                if row:
                    d = dict(row)
                    d["match_type"] = "semantic"
                    d["distance"] = r["distance"]
                    merged.append(d)
        except Exception:
            pass

    merged.sort(key=lambda x: x.get("start_dt") or "")
    return merged[:limit]


def search_documents_tool(
    query: str,
    source: str = None,
    limit: int = 10,
) -> list[dict]:
    where = {"source": source} if source else None
    hits = semantic_search("documents", query, n_results=limit, where=where)
    out = []
    with get_db() as conn:
        for h in hits:
            row = conn.execute(
                "SELECT id, title, path, source, word_count, substr(COALESCE(body_text,''),1,400) AS body_preview "
                "FROM documents WHERE id=?",
                (h["id"],),
            ).fetchone()
            if row:
                d = dict(row)
                d["match_type"] = "semantic"
                d["distance"] = h["distance"]
                d["snippet"] = (h.get("document") or "")[:500]
                out.append(d)
            else:
                out.append({"match_type": "semantic", "id": h["id"], "distance": h["distance"], "snippet": h.get("document", "")[:500]})
    return out


def search_amazon_orders(
    start_date: str = None,
    end_date: str = None,
    item_query: str = None,
    limit: int = 20,
) -> list[dict]:
    conditions = []
    params = []

    if start_date:
        conditions.append(
            "(order_date IS NOT NULL AND LENGTH(order_date) >= 10 AND date(order_date) >= date(?))"
        )
        params.append(start_date)
    if end_date:
        conditions.append(
            "(order_date IS NOT NULL AND LENGTH(order_date) >= 10 AND date(order_date) <= date(?))"
        )
        params.append(end_date)
    if item_query:
        conditions.append("LOWER(items) LIKE ?")
        params.append(f"%{item_query.lower()}%")

    where_sql = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT id, order_date, total_amount, currency, status, items, source_email_id
        FROM amazon_orders
        WHERE {where_sql}
        ORDER BY order_date DESC
        LIMIT ?
    """
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_all(query: str, limit: int = 5) -> dict:
    results = {}
    for collection in ["emails", "ai_conversations", "documents", "bookmarks", "calendar"]:
        try:
            hits = semantic_search(collection, query, n_results=limit)
            if hits:
                results[collection] = hits
        except Exception:
            pass
    return results


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/mcp")
def mcp_endpoint(request: MCPRequest):
    method = request.method

    if method == "tools/list":
        return {"result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = request.params.get("name")
        arguments = request.params.get("arguments", {})

        try:
            if tool_name == "search_transactions":
                result = search_transactions(**arguments)
            elif tool_name == "get_spending_summary":
                result = get_spending_summary(**arguments)
            elif tool_name == "get_account_balances":
                result = get_account_balances()
            elif tool_name == "search_emails":
                result = search_emails_tool(**arguments)
            elif tool_name == "get_recent_emails":
                result = get_recent_emails(**arguments)
            elif tool_name == "search_calendar_events":
                result = search_calendar_events(**arguments)
            elif tool_name == "search_documents":
                result = search_documents_tool(**arguments)
            elif tool_name == "search_amazon_orders":
                result = search_amazon_orders(**arguments)
            elif tool_name == "search_all":
                result = search_all(**arguments)
            else:
                return {"error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

            return {
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
                }
            }

        except Exception as e:
            return {"error": {"code": -32000, "message": str(e)}}

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("MCP_PORT", 8000))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    print(f"Starting MCP server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
