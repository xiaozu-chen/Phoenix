import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from settings.config import Config
from memory.db import db, embed, recall
from memory.context import (
    format_env_time,
    should_include_environment,
    pop_interruption_block,
)
from hook.registry import emit
from settings.schemas import ChatIn
from settings.prompt import load_system_prompt
from sub.logger import log, log_exception
from sub import console
from llm.lifecycle import ensure_running, touch as touch_llm
from sub.activity import begin as _activity_begin, end as _activity_end

router = APIRouter()

# The base system prompt now lives in a plain-text file under data/ (see
# settings/prompt.py), created with a default the first time the app runs
# with none present. It's editable live from the Configuration window's
# Prompt editor tab -- load_system_prompt() re-reads it on every turn so
# edits take effect on the very next message, no restart needed.

MAIN_TEMPERATURE = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mem_block(memories) -> str:
    if not memories:
        return ""
    return "\n\n[Relevant memories]\n" + "\n".join(
        f"- ({role}) {content}" for _, role, content in memories
    )


def _build_messages(recent, context_blocks: list[str]):
    persona_extra = (Config.persona or "").strip()
    system = load_system_prompt()
    if persona_extra:
        system += f"\n\n[Persona notes]\n{persona_extra}"
    system += "".join(b for b in context_blocks if b)
    messages = [{"role": "system", "content": system}]
    for _, role, content in recent:
        messages.append(
            {"role": "assistant" if role == "assistant" else "user", "content": content}
        )
    return messages


def _complete(messages, temperature: float) -> str:
    ensure_running()
    r = httpx.post(
        f"{Config.llama_url}/v1/chat/completions",
        json={"messages": messages, "stream": False, "temperature": temperature},
        timeout=120,
    )
    r.raise_for_status()
    touch_llm()
    return r.json()["choices"][0]["message"]["content"]


def _generate(inp: ChatIn):
    console.info(f"Chat: processing message ({len(inp.message)} chars)...")
    t0 = time.time()
    conn = db()
    _activity_begin("Replying to chat...")
    try:
        try:
            prev_row = conn.execute(
                "SELECT ts FROM messages ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_ts = prev_row[0] if prev_row else None

            user_emb = embed(inp.message)
            conn.execute(
                "INSERT INTO messages(role,content,embedding,ts) VALUES(?,?,?,?)",
                ("user", inp.message, user_emb, _now()),
            )
            conn.commit()
            emit("chat.message", message=inp.message)

            recent = conn.execute(
                "SELECT id, role, content FROM messages ORDER BY id DESC LIMIT ?",
                (Config.context_window,),
            ).fetchall()[::-1]
            recent_ids = {r[0] for r in recent}

            memories = recall(conn, user_emb, recent_ids)

            yield f"data: {json.dumps({'status': 'read'})}\n\n"

            current_time = time.time()
            show_env = should_include_environment(prev_ts, current_time)
            if show_env:
                yield f"data: {json.dumps({'status': 'timestamp', 'time': current_time})}\n\n"

            context_blocks = [_mem_block(memories)]
            if show_env:
                context_blocks.append(f"\n\n[Right now]\n{format_env_time(datetime.now())}")

            interrupt_block = pop_interruption_block()
            if interrupt_block:
                context_blocks.append(f"\n\n[You just got interrupted]\n{interrupt_block}")

            base_messages = _build_messages(recent, context_blocks)

            reply = _complete(base_messages, MAIN_TEMPERATURE)

            try:
                reply_emb = embed(reply)
            except Exception as e:
                reply_emb = None
                log(target_log="session", log_type="warning", message=f"Failed to generate embedding for reply: {e}")

            conn.execute(
                "INSERT INTO messages(role,content,embedding,ts) VALUES(?,?,?,?)",
                ("assistant", reply, reply_emb, _now()),
            )
            conn.commit()
            emit("chat.reply", message=inp.message, reply=reply)

            yield f"data: {json.dumps({'status': 'done', 'reply': reply})}\n\n"

            Path(Config.data_path).parent.mkdir(parents=True, exist_ok=True)
            with open(Config.data_path, "a") as f:
                to_write = json.dumps(
                    {
                        "conversation": [
                            {"role": "user", "content": inp.message},
                            {"role": "assistant", "content": reply},
                        ]
                    }
                )
                f.write(f"{to_write}\n")
                log(target_log="system", log_type="info", message=f"Successfully written turn data to {Config.data_path}")

            console.success(f"Chat: replied in {time.time() - t0:.2f}s")

        except Exception:
            # Anything that blows up mid-stream (llama-server down,
            # embedding call failing, a tool raising, disk full while
            # writing data.jsonl, ...) used to just die silently as far
            # as the terminal was concerned -- the client would see a
            # cut-off stream and there'd be nothing on screen explaining
            # why. Show the real traceback and tell the client too,
            # instead of leaving both sides guessing.
            detail = log_exception("system", "Chat generation failed")
            console.error(f"Chat: failed after {time.time() - t0:.2f}s")
            yield f"data: {json.dumps({'status': 'error', 'detail': detail})}\n\n"

    finally:
        _activity_end("Replying to chat...")
        conn.close()


@router.post("/chat")
def chat(inp: ChatIn):
    return StreamingResponse(_generate(inp), media_type="text/event-stream")


@router.get("/history")
def history():
    conn = db()
    rows = conn.execute("SELECT role, content FROM messages ORDER BY id ASC").fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in rows]


@router.post("/reset")
def reset():
    conn = db()
    conn.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    emit("chat.reset")
    return {"ok": True}