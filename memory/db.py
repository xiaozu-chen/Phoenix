import sqlite3
import struct
import math
import httpx
from pathlib import Path

from settings.config import Config
from llm.lifecycle import ensure_running, touch as touch_llm

def db() -> sqlite3.Connection:
    Path(Config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Config.db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             role TEXT NOT NULL,
             content TEXT NOT NULL,
             embedding BLOB,
             ts TEXT NOT NULL)"""
    )
    return conn


def embed(text: str) -> bytes:
    ensure_running()
    r = httpx.post(f"{Config.llama_url}/embedding", json={"content": text}, timeout=60)
    r.raise_for_status()
    touch_llm()
    data = r.json()

    if isinstance(data, list):
        data = data[0]

    vec = data["embedding"]

    if vec and isinstance(vec[0], list):
        vec = vec[0]

    return struct.pack(f"{len(vec)}f", *vec)

def cosine(a: bytes, b: bytes) -> float:
    """
    Computes the cosine similarity between two byte-encoded float vectors (va and vb).

    Visual Formula:
                  va · vb            Σ (x_i * y_i)
    cos(θ) = ───────────────  =  ───────────────────────
              ||va|| * ||vb||    √(Σ x_i²) * √(Σ y_i²)

              [va] ─── (Vector A)
             /
            /  θ (angle)
           /─────────── [vb] ─── (Vector B)

    Values range from -1.0 (opposite) to 1.0 (identical direction).
    """
    n = min(len(a), len(b)) // 4
    va = struct.unpack(f"{n}f", a[: n * 4])
    vb = struct.unpack(f"{n}f", b[: n * 4])
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va)) or 1e-8
    nb = math.sqrt(sum(x * x for x in vb)) or 1e-8
    return dot / (na * nb)


def recall(conn, query_emb: bytes, exclude_ids: set, k: int = Config.memory_topk):
    rows = conn.execute(
        "SELECT id, role, content, embedding FROM messages ORDER BY id DESC LIMIT ?",
        (Config.recall_scan_limit,),
    ).fetchall()

    scored = [
        (cosine(query_emb, emb), role, content)
        for rid, role, content, emb in rows
        if rid not in exclude_ids and emb is not None
    ]

    scored.sort(key=lambda x: -x[0])
    return scored[:k]
