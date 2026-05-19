#!/usr/bin/env python3
"""
mcp_stdio.py

Self-contained MCP stdio server for Claude Desktop.
Reads newline-delimited JSON-RPC 2.0 from stdin, writes responses to stdout.
No external HTTP server required.

Claude Desktop config:
  {
    "mcpServers": {
      "secondbrain": {
        "command": "python",
        "args": ["/path/to/secondbrain/mcp_stdio.py"]
      }
    }
  }
"""

import json
import os
import sys

# Allow importing storage modules from the repo root
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "config", "secrets.env"))

from storage.store import get_db, semantic_search  # noqa: E402

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
            },
        },
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
                    "description": "How to group results",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_account_balances",
        "description": "Get current balances for all linked bank and credit card accounts.",
        "inputSchema": {"type": "object", "properties": {}},
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
            "required": ["query"],
        },
    },
    {
        "name": "get_recent_emails",
        "description": "Get the most recent emails, optionally filtered by sender.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of emails (default 10)"},
                "from_address": {"type": "string", "description": "Filter by sender"},
            },
        },
    },
    {
        "name": "search_calendar_events",
        "description": (
            "Search Google Calendar events by date range and/or keyword."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
                "keyword": {"type": "string", "description": "Keyword for title, description, or location"},
                "limit": {"type": "integer", "description": "Max results (default 25)"},
            },
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Semantic search across ingested documents and notes (Google Drive, Apple Notes, etc.). "
            "Use for 'find my notes about taxes', 'which doc mentions the lease'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "source": {"type": "string", "description": "Optional filter: gdrive | apple_notes | local | bookmark"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_amazon_orders",
        "description": "Look up Amazon orders parsed from email by order date or item text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Order date on/after YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "Order date on/before YYYY-MM-DD"},
                "item_query": {"type": "string", "description": "Substring to match in line items"},
                "limit": {"type": "integer", "description": "Max rows (default 20)"},
            },
        },
    },
    {
        "name": "search_all",
        "description": (
            "Search across ALL data — emails, AI chats, documents, bookmarks, calendar. "
            "Use when you don't know where something is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "integer", "description": "Max results per source (default 5)"},
            },
            "required": ["query"],
        },
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
        sql = ("SELECT id, from_address, from_name, subject, date, snippet FROM emails "
               "WHERE (subject LIKE ? OR snippet LIKE ?)")
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


def get_recent_emails(limit=10, from_address=None):
    with get_db() as conn:
        if from_address:
            rows = conn.execute(
                "SELECT id, from_address, from_name, subject, date, snippet "
                "FROM emails WHERE from_address LIKE ? ORDER BY date DESC LIMIT ?",
                (f"%{from_address}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, from_address, from_name, subject, date, snippet "
                "FROM emails ORDER BY date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def search_calendar_events(start_date=None, end_date=None, keyword=None, limit=25):
    conditions, params = [], []
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
        FROM calendar_events WHERE {where_sql} ORDER BY start_dt ASC LIMIT ?
    """
    params.append(limit)
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    seen = {r["id"] for r in rows}
    merged = list(rows)
    if keyword:
        try:
            for r in semantic_search("calendar", keyword, n_results=limit):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT id, calendar_id, title, description, location, "
                        "start_dt, end_dt, all_day, status FROM calendar_events WHERE id=?",
                        (r["id"],),
                    ).fetchone()
                if row:
                    d = dict(row)
                    d["match_type"] = "semantic"
                    merged.append(d)
        except Exception:
            pass
    merged.sort(key=lambda x: x.get("start_dt") or "")
    return merged[:limit]


def search_documents_tool(query, source=None, limit=10):
    where = {"source": source} if source else None
    hits = semantic_search("documents", query, n_results=limit, where=where)
    out = []
    with get_db() as conn:
        for h in hits:
            row = conn.execute(
                "SELECT id, title, path, source, word_count, "
                "substr(COALESCE(body_text,''),1,400) AS body_preview "
                "FROM documents WHERE id=?",
                (h["id"],),
            ).fetchone()
            if row:
                d = dict(row)
                d["match_type"] = "semantic"
                d["snippet"] = (h.get("document") or "")[:500]
                out.append(d)
            else:
                out.append({"match_type": "semantic", "id": h["id"],
                            "snippet": h.get("document", "")[:500]})
    return out


def search_amazon_orders(start_date=None, end_date=None, item_query=None, limit=20):
    conditions, params = [], []
    if start_date:
        conditions.append("(order_date IS NOT NULL AND LENGTH(order_date) >= 10 AND date(order_date) >= date(?))")
        params.append(start_date)
    if end_date:
        conditions.append("(order_date IS NOT NULL AND LENGTH(order_date) >= 10 AND date(order_date) <= date(?))")
        params.append(end_date)
    if item_query:
        conditions.append("LOWER(items) LIKE ?")
        params.append(f"%{item_query.lower()}%")
    where_sql = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT id, order_date, total_amount, currency, status, items, source_email_id
        FROM amazon_orders WHERE {where_sql} ORDER BY order_date DESC LIMIT ?
    """
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_all(query, limit=5):
    results = {}
    for collection in ["emails", "ai_conversations", "documents", "bookmarks", "calendar"]:
        try:
            hits = semantic_search(collection, query, n_results=limit)
            if hits:
                results[collection] = hits
        except Exception:
            pass
    return results


# ------------------------------------------------------------------ #
# JSON-RPC 2.0 / MCP dispatch                                         #
# ------------------------------------------------------------------ #

NOTIFICATION_METHODS = {
    "notifications/initialized",
    "notifications/cancelled",
    "notifications/progress",
}


def dispatch(req: dict):
    """Return a response dict, or None for notifications."""
    method = req.get("method", "")
    params = req.get("params") or {}
    req_id = req.get("id")

    # Notifications never get a response
    if method in NOTIFICATION_METHODS or ("id" not in req and method.startswith("notifications/")):
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "secondbrain", "version": "1.0.0"},
        })

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_transactions":
                result = search_transactions(**args)
            elif name == "get_spending_summary":
                result = get_spending_summary(**args)
            elif name == "get_account_balances":
                result = get_account_balances()
            elif name == "search_emails":
                result = search_emails_tool(**args)
            elif name == "get_recent_emails":
                result = get_recent_emails(**args)
            elif name == "search_calendar_events":
                result = search_calendar_events(**args)
            elif name == "search_documents":
                result = search_documents_tool(**args)
            elif name == "search_amazon_orders":
                result = search_amazon_orders(**args)
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
# Stdio main loop                                                      #
# ------------------------------------------------------------------ #

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        try:
            resp = dispatch(req)
        except Exception as e:
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32000, "message": str(e)}}

        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
