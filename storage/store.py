"""
storage/store.py

Shared helpers for reading/writing SQLite and ChromaDB.
Import this everywhere instead of opening connections directly.
"""

import sqlite3
import os
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
from dotenv import load_dotenv

load_dotenv("config/secrets.env")

DB_PATH = os.getenv("DB_PATH", "storage/secondbrain.db")
CHROMA_PATH = os.getenv("CHROMA_PATH", "storage/chroma")
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")

# Lazy-loaded singletons
_embedder: Optional[SentenceTransformer] = None
_chroma_client: Optional[chromadb.PersistentClient] = None


# ------------------------------------------------------------------ #
# SQLite                                                               #
# ------------------------------------------------------------------ #

@contextmanager
def get_db():
    """Context manager for SQLite connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row   # access columns by name
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_ingest_start(source: str) -> int:
    """Record the start of an ingest run. Returns log row id."""
    from datetime import datetime, timezone
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_log (source, started_at) VALUES (?, ?)",
            (source, datetime.now(timezone.utc).isoformat())
        )
        return cur.lastrowid


def log_ingest_finish(log_id: int, added: int, updated: int, error: str = None):
    """Update the ingest log row with results."""
    from datetime import datetime, timezone
    status = "error" if error else "success"
    with get_db() as conn:
        conn.execute("""
            UPDATE ingest_log
            SET finished_at=?, records_added=?, records_updated=?, status=?, error_message=?
            WHERE id=?
        """, (datetime.now(timezone.utc).isoformat(), added, updated, status, error, log_id))


# ------------------------------------------------------------------ #
# ChromaDB + Embeddings                                                #
# ------------------------------------------------------------------ #

def get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        print(f"Loading embedding model: {EMBED_MODEL}")
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def get_chroma() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_PATH,
            settings=Settings(anonymized_telemetry=False)
        )
    return _chroma_client


def get_collection(name: str):
    """Get or create a named ChromaDB collection."""
    client = get_chroma()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"}
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts. Returns list of float vectors."""
    embedder = get_embedder()
    return embedder.encode(texts, show_progress_bar=len(texts) > 20).tolist()


def upsert_to_chroma(
    collection_name: str,
    ids: list[str],
    texts: list[str],
    metadatas: list[dict]
):
    """Embed and upsert documents into a ChromaDB collection."""
    if not texts:
        return
    collection = get_collection(collection_name)
    embeddings = embed_texts(texts)
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas
    )


def semantic_search(
    collection_name: str,
    query: str,
    n_results: int = 10,
    where: dict = None
) -> list[dict]:
    """
    Search a ChromaDB collection by semantic similarity.
    Returns list of {id, document, metadata, distance}.
    """
    collection = get_collection(collection_name)
    query_embedding = embed_texts([query])[0]
    kwargs = {"query_embeddings": [query_embedding], "n_results": n_results}
    if where:
        kwargs["where"] = where

    results = collection.query(**kwargs)

    output = []
    for i in range(len(results["ids"][0])):
        output.append({
            "id": results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
        })
    return output


# ------------------------------------------------------------------ #
# Utilities                                                            #
# ------------------------------------------------------------------ #

def stable_id(*parts: str) -> str:
    """Generate a stable hash ID from one or more strings."""
    combined = "|".join(str(p) for p in parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
