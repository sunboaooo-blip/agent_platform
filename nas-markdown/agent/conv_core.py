#!/usr/bin/env python3
"""
conv_core.py — shared conversation logic for the overnight agent.

Both wake-up channels (NAS conversation loop + SSH ask) use this so they
share one conversation history and one Qwen calling path.

The agent "works under Qwen": the local Qwen3.8-27B (127.0.0.1:8000) is the
reasoning engine. This module only orchestrates: history, prompt, Qwen call,
and the recommend-only red line.
"""
import json, os, time, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
HERE = os.path.dirname(os.path.abspath(__file__))
HIST_PATH = os.path.join(HERE, "conversation", "history.jsonl")
CTX_DIR = os.path.join(HERE, "context")
LOG_PATH = os.path.join(HERE, "logs", "conversation.log")

LLM_URL = "http://127.0.0.1:8000/v1"
MODEL = "qwen3.8-27b"
MAX_HISTORY = 24  # last N turns kept in the prompt (KV pool is shared)

SYSTEM = """You are the overnight decision agent for a trading portfolio, running on a
local GPU node. You reason with the local Qwen model and you are being
conversational with the operator (bo).

HARD RULES:
1. You RECOMMEND. You never execute. The control plane (mini PC) owns
   execution, risk limits and human confirmation.
2. Base answers ONLY on data you are given or that is in the portfolio
   context. If data is missing or stale, say so and prefer "hold".
3. Prefer "hold" when uncertain. A missed move costs less than a wrong trade.
4. Be concise and direct. No filler. Answer in the language the operator used.
5. When the operator asks for a decision, give: action (act/hold/monitor),
   confidence, the specific positions/sizes, and the key risk flags.
"""

def log(msg):
    line = f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def load_history():
    if not os.path.exists(HIST_PATH):
        return []
    out = []
    with open(HIST_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out

def append_history(role, content, meta=None):
    os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
    rec = {"ts": datetime.now(JST).isoformat(), "role": role, "content": content}
    if meta:
        rec["meta"] = meta
    with open(HIST_PATH, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def portfolio_context():
    p = os.path.join(CTX_DIR, "portfolio.md")
    if os.path.exists(p):
        with open(p) as f:
            return f.read()[:30000]
    return "(no portfolio context yet — treat as insufficient data, prefer hold)"

def call_qwen(messages, max_tokens=2048, temperature=0.3):
    """Call the local Qwen chat endpoint. Returns content string or None."""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        LLM_URL + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer not-needed"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            reply = json.loads(r.read())
    except Exception as e:
        log(f"Qwen request failed: {e}")
        return None
    try:
        choice = reply["choices"][0]
        content = (choice.get("message", {}).get("content") or "").strip()
        if not content:
            log(f"Qwen empty content (finish_reason={choice.get('finish_reason')})")
            return None
        return content
    except (KeyError, IndexError) as e:
        log(f"Qwen reply malformed: {e}")
        return None

def build_messages(user_text):
    """Assemble the message list: system + portfolio + recent history + new user turn."""
    msgs = [{"role": "system", "content": SYSTEM + "\n\nPORTFOLIO CONTEXT:\n" + portfolio_context()}]
    hist = load_history()[-MAX_HISTORY:]
    for h in hist:
        role = "user" if h["role"] == "user" else "assistant"
        msgs.append({"role": role, "content": h["content"]})
    msgs.append({"role": "user", "content": user_text})
    return msgs

def answer(user_text, meta=None):
    """One full turn: record the user message, call Qwen, record the reply.
    Returns the reply string (or an error note)."""
    append_history("user", user_text, meta)
    reply = call_qwen(build_messages(user_text))
    if reply is None:
        note = "(agent error: Qwen did not return a usable reply — check the LLM is up: `llm-switch vllm`)"
        append_history("assistant", note, {"error": True})
        return note
    append_history("assistant", reply, meta)
    return reply
