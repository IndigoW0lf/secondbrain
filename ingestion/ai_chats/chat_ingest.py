"""
ingestion/ai_chats/chat_ingest.py

Ingests exported chat history from Claude and ChatGPT.
Embeds conversations into ChromaDB for semantic search.

EXPORT INSTRUCTIONS:

  Claude:
    1. Go to claude.ai → Settings → Privacy
    2. Click "Export data" → downloads a ZIP
    3. Unzip → you'll have conversations.json
    4. Run: python ingestion/ai_chats/chat_ingest.py --file path/to/conversations.json --source claude

  ChatGPT:
    1. Go to chat.openai.com → Settings → Data controls
    2. Click "Export data" → email arrives with ZIP
    3. Unzip → you'll have conversations.json
    4. Run: python ingestion/ai_chats/chat_ingest.py --file path/to/conversations.json --source chatgpt

  Gemini:
    1. Go to takeout.google.com
    2. Select "Gemini Apps Activity" → Export
    3. Run: python ingestion/ai_chats/chat_ingest.py --file path/to/gemini.json --source gemini
"""

import os
import sys
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import track

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from storage.store import get_db, upsert_to_chroma, log_ingest_start, log_ingest_finish, stable_id

console = Console()


# ------------------------------------------------------------------ #
# Parsers for each platform's export format                           #
# ------------------------------------------------------------------ #

def parse_claude(data: list) -> list[dict]:
    """Parse Claude's conversations.json export."""
    conversations = []
    for convo in data:
        messages = []
        for msg in convo.get("chat_messages", []):
            content = ""
            # Claude export nests content in an array
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    content += block.get("text", "")
                elif isinstance(block, str):
                    content += block
            messages.append({
                "role": msg.get("sender", "unknown"),
                "content": content.strip(),
                "created_at": msg.get("created_at", ""),
            })

        conversations.append({
            "id": convo.get("uuid", stable_id(convo.get("name", ""), str(convo.get("created_at", "")))),
            "title": convo.get("name", "Untitled"),
            "created_at": convo.get("created_at", ""),
            "updated_at": convo.get("updated_at", ""),
            "messages": messages,
        })
    return conversations


def parse_chatgpt(data: list) -> list[dict]:
    """Parse ChatGPT's conversations.json export."""
    conversations = []
    for convo in data:
        messages = []
        # ChatGPT stores messages in a dict keyed by id, ordered by create_time
        mapping = convo.get("mapping", {})
        nodes = sorted(mapping.values(), key=lambda x: x.get("message", {}) and x["message"].get("create_time") or 0)

        for node in nodes:
            msg = node.get("message")
            if not msg or not msg.get("content"):
                continue
            role = msg.get("author", {}).get("role", "unknown")
            if role == "system":
                continue
            parts = msg["content"].get("parts", [])
            content = " ".join(str(p) for p in parts if isinstance(p, str))
            if not content.strip():
                continue
            messages.append({
                "role": role,
                "content": content.strip(),
                "created_at": datetime.fromtimestamp(
                    msg.get("create_time") or 0, tz=timezone.utc
                ).isoformat(),
            })

        conversations.append({
            "id": convo.get("id", stable_id(convo.get("title", ""), str(convo.get("create_time", "")))),
            "title": convo.get("title", "Untitled"),
            "created_at": datetime.fromtimestamp(convo.get("create_time") or 0, tz=timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(convo.get("update_time") or 0, tz=timezone.utc).isoformat(),
            "messages": messages,
        })
    return conversations


PARSERS = {
    "claude": parse_claude,
    "chatgpt": parse_chatgpt,
    # "gemini": parse_gemini,  # add when needed
}


# ------------------------------------------------------------------ #
# Ingest                                                               #
# ------------------------------------------------------------------ #

def run(file_path: str, source: str):
    log_id = log_ingest_start(f"ai_chats_{source}")
    console.print(f"\n[bold]AI chat ingest: {source} from {file_path}[/]")

    if source not in PARSERS:
        console.print(f"[red]Unknown source '{source}'. Supported: {list(PARSERS)}[/]")
        return

    raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
    conversations = PARSERS[source](raw)
    console.print(f"  Found {len(conversations)} conversations")

    added = 0
    embed_ids, embed_texts, embed_metas = [], [], []

    for convo in track(conversations, description="Storing..."):
        conv_id = f"{source}_{convo['id']}"

        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM ai_conversations WHERE id=?", (conv_id,)
            ).fetchone()

            if not existing:
                conn.execute("""
                    INSERT OR IGNORE INTO ai_conversations
                        (id, source, title, created_at, updated_at, message_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    conv_id, source, convo["title"],
                    convo["created_at"], convo["updated_at"],
                    len(convo["messages"])
                ))

                for i, msg in enumerate(convo["messages"]):
                    msg_id = f"{conv_id}_msg_{i}"
                    conn.execute("""
                        INSERT OR IGNORE INTO ai_messages
                            (id, conversation_id, role, content, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (msg_id, conv_id, msg["role"], msg["content"], msg["created_at"]))

                added += 1

                # Build embed text: title + first user message + first assistant response
                user_msgs = [m["content"] for m in convo["messages"] if m["role"] in ("user", "human")]
                asst_msgs = [m["content"] for m in convo["messages"] if m["role"] == "assistant"]
                embed_text = f"Title: {convo['title']}\n\n"
                if user_msgs:
                    embed_text += f"User: {user_msgs[0][:500]}\n\n"
                if asst_msgs:
                    embed_text += f"Assistant: {asst_msgs[0][:500]}"

                embed_ids.append(conv_id)
                embed_texts.append(embed_text)
                embed_metas.append({
                    "source": source,
                    "title": convo["title"],
                    "created_at": convo["created_at"][:10] if convo["created_at"] else "",
                    "message_count": str(len(convo["messages"])),
                })

    if embed_ids:
        console.print(f"Embedding {len(embed_ids)} conversations...")
        upsert_to_chroma("ai_conversations", embed_ids, embed_texts, embed_metas)
        with get_db() as conn:
            conn.executemany(
                "UPDATE ai_conversations SET embedded=1 WHERE id=?",
                [(eid,) for eid in embed_ids]
            )

    console.print(f"[bold green]Done.[/] {added} new conversations stored.")
    log_ingest_finish(log_id, added, 0)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="Path to exported conversations.json")
    parser.add_argument("--source", required=True, choices=["claude", "chatgpt", "gemini"])
    args = parser.parse_args()
    run(file_path=args.file, source=args.source)
