#!/usr/bin/env python3
"""hive-watch.py -- the receive side of the hive.

Polls one Discord text channel over plain REST (no websockets, no discord.py),
skips anything we posted ourselves, appends everything else to a JSONL inbox,
and optionally answers with the local llama.cpp server.

    hive-watch.py --test          auth + channel access check, then exit
    hive-watch.py --once          one poll, print what arrived, exit
    hive-watch.py --dry-run       poll forever, print, never call LLM, never post
    hive-watch.py                 poll forever and answer
    hive-watch.py --respond-all   answer every message from anyone
    hive-watch.py --require-mention  answer only when @-mentioned (default)

Cursor: state/last_id is the id of the newest message already handled. If it is
missing or corrupt the poller REFUSES to start rather than quietly baselining at
the newest message, which would discard everything posted while it was stopped.
Choose one explicitly: --catch-up N to ingest the gap first, or --bootstrap to
accept it. A corrupt file is moved to last_id.bad-<ts> instead of overwritten.

Why REST polling instead of the gateway: the bot does not have (and does not
need) a verified application, and stdlib HTTP is all we need. Caveat measured
2026-09-13: Message Content Intent gates REST reads too, not just gateway
events -- with the intent off, foreign messages return content='' silently.
The intent is ON for this bot; --diagnose re-checks it, and the poller warns
on empty foreign messages.

Exit codes: 0 ok / clean shutdown, 1 config error, 2 Discord API error,
3 LLM error.
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
HERE = Path(__file__).resolve().parent
# Paths are resolved through functions, not constants, so a caller (hive-test.py)
# can redirect them without an import-order dance.
STATE_DIR = Path(os.environ.get("HIVE_STATE_DIR", str(HERE / "state")))
LOG_DIR = Path(os.environ.get("HIVE_LOG_DIR", str(HERE / "logs")))


def inbox_path():   return STATE_DIR / "inbox.jsonl"
def history_path(): return STATE_DIR / "history.json"
def last_id_path(): return STATE_DIR / "last_id"
def log_file():     return LOG_DIR / "watch.log"
CONF_DIR = Path(os.environ.get("HIVE_CONF_DIR", str(Path.home() / ".config" / "discord")))

DEFAULT_SYSTEM = """You are "qwen local agent", one of several agents sharing a \
Discord channel. Other agents and a human (bo) read and write there.

You are seeing this message because it was already routed to you: it either \
@-mentions you, starts with the !hive prefix, or replies to something you said. \
Treat it as addressed to you and answer it.

Rules for every reply:
- Plain text. Discord markdown is fine; no code fences unless code is asked for.
- Short. Aim for 1-6 sentences unless the question needs more.
- Never write @everyone or @here. Never paste tokens, webhook URLs, key files, \
or anything from ~/.config/.
- You run on bo's local GPU node. You can state facts about this machine, but \
you cannot execute anything from here.
- NO_REPLY exists only for the rare case where the message is plainly meant for \
a different named agent, or is pure chatter between others that asks nothing of \
you. When in doubt, answer.
- Never suggest using a user token to read Discord. Only bot tokens and webhooks.
- Output only the message itself. No preamble, no restating the question, no \
"we need to answer" narration.
"""

running = True


# ---------------------------------------------------------------- logging
def log(msg, level="info"):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {level:<5s} {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_file().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def shutdown(signum, _frame):
    global running
    log(f"signal {signum}, finishing up")
    running = False


# ---------------------------------------------------------------- config
def read_conf_file(path):
    """Parse the `key "value"` files in ~/.config/discord/."""
    out = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # `key "value"`, `key value` and `key"value"` are all accepted -- the
        # real discord file is written as ID"123" with no separator, and a
        # silently empty app_id disables self-detection and mention matching.
        m = re.match(r'^([A-Za-z_]\w*)[ \t]*=?[ \t]*(.*)$', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val.strip()
    return out


def load_secrets():
    bot = read_conf_file(CONF_DIR / "discord")
    wh = read_conf_file(CONF_DIR / "webhook")
    return {
        "token": os.environ.get("HIVE_BOT_TOKEN") or bot.get("token", ""),
        "app_id": os.environ.get("HIVE_APP_ID") or bot.get("ID", ""),
        "webhook": os.environ.get("HIVE_WEBHOOK") or wh.get("url", ""),
    }


def webhook_id_from(url):
    m = re.search(r"/webhooks/(\d+)/", url or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- discord
class Discord:
    """Tiny REST client with 429 backoff."""

    def __init__(self, token):
        self.token = token
        self.mute_until = 0.0

    def request(self, method, path, payload=None, timeout=20, _retry=0):
        if time.time() < self.mute_until:
            time.sleep(self.mute_until - time.time() + 0.05)
        headers = {"Authorization": f"Bot {self.token}",
                   "User-Agent": "hive-node/0.1 (+https://github.com/OWNER/hive)"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API + path, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                wait = _retry_after(body, e.headers)
                if _retry < 4:
                    log(f"HTTP 429, backing off {wait:.1f}s", "warn")
                    time.sleep(wait)
                    return self.request(method, path, payload, timeout, _retry + 1)
                self.mute_until = time.time() + wait
            return e.code, body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 0, f"{type(e).__name__}: {e}"

    def whoami(self):
        s, b = self.request("GET", "/users/@me")
        if s != 200:
            return None, f"HTTP {s} {b[:200]}"
        return json.loads(b), ""

    def messages(self, channel, after=None, limit=50):
        q = f"?limit={limit}"
        if after:
            q += f"&after={after}"
        s, b = self.request("GET", f"/channels/{channel}/messages{q}")
        if s != 200:
            return None, f"HTTP {s} {b[:200]}"
        return json.loads(b), ""

    def send(self, channel, content):
        payload = {"content": content[:2000], "allowed_mentions": {"parse": []}}
        return self.request("POST", f"/channels/{channel}/messages", payload)


def _retry_after(body, headers):
    try:
        return min(float(json.loads(body).get("retry_after", 2)), 60.0)
    except Exception:
        try:
            return min(float(headers.get("Retry-After", 2)), 60.0)
        except Exception:
            return 2.0


def post_webhook(url, content, username):
    payload = {"username": username, "content": content[:2000],
               "allowed_mentions": {"parse": []}}
    req = urllib.request.Request(
        url + "?wait=true", method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "hive-node/0.1 (+https://github.com/OWNER/hive)"},
        data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except (urllib.error.URLError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- helpers
def defang(text):
    for tok in ("@everyone", "@here"):
        text = text.replace(tok, tok[:1] + "\u200b" + tok[1:])
    return text


def chunk(text, limit=2000):
    out = []
    text = text.strip("\n")
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit                       # no newline anywhere: hard split
        elif cut < limit // 2 and len(text[cut + 1:]) > limit:
            cut = limit                       # newline too early to be useful
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    return out


def snowflake_ms(mid):
    return (int(mid) >> 22) + 1420070400000


def stamp(msg):
    ts = msg.get("timestamp")
    if ts:
        return ts[11:16]
    return time.strftime("%H:%M", time.localtime(snowflake_ms(msg["id"]) / 1000))


def author_name(msg):
    a = msg.get("author") or {}
    return a.get("global_name") or a.get("username") or "?"


def is_mine(msg, bot_id, wh_id):
    a = (msg.get("author") or {}).get("id", "")
    if wh_id and msg.get("webhook_id") == wh_id:
        return True
    if bot_id and (a == bot_id or msg.get("webhook_id") == bot_id):
        return True
    return False


def _atomic_write(path, text):
    """temp file -> fsync -> os.replace -> fsync the directory.

    The cursor and history.json used to go out through write_text(), which
    truncates first: a crash or ENOSPC part-way through leaves a short file
    behind. For the cursor that is the worst case rather than a rare one -- a
    *numeric* but truncated id is indistinguishable from a legitimate one, so
    the poller resumes from an earlier point and re-ingests (duplicates), while a
    non-numeric remnant falls straight into the silent-loss baseline path.
    os.replace is atomic on POSIX, so a reader sees either the old file or the
    new one, never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:  # make the rename itself durable, not just the bytes
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # some filesystems refuse fsync on a directory; data is already safe


def load_last_id():
    try:
        v = last_id_path().read_text().strip()
        return v if v.isdigit() else None
    except OSError:
        return None


def cursor_problem():
    """Why there is no usable cursor, or "" when there is one.

    Telling "absent" (genuinely the first run) apart from "corrupt" matters: the
    second means messages were already ingested once, so re-baselining at the
    newest message will silently drop whatever arrived since and must never be
    treated as a fresh start.
    """
    p = last_id_path()
    if not p.exists():
        return "absent"
    try:
        v = p.read_text().strip()
    except OSError as e:
        return f"unreadable ({type(e).__name__})"
    if not v:
        return "empty"
    if not v.isdigit():
        return f"corrupt (found {v[:24]!r})"
    return ""


def check_cursor(cfg):
    """Decide what to do about a missing/damaged cursor. 0 = proceed, 1 = refuse.

    A missing cursor used to fall into the same silent baseline as a genuine
    first run, so anything posted while the watcher was down was dropped without
    a word. Splitting "absent" from "corrupt" and requiring an explicit
    --catch-up or --bootstrap turns that accident into a decision.
    """
    prob = cursor_problem()
    if not prob:
        log(f"cursor at message {load_last_id()}")
        return 0
    if not (cfg.catch_up or getattr(cfg, "bootstrap", False)):
        return refuse_no_cursor()
    if prob != "absent":
        bad = preserve_bad_cursor()
        log(f"cursor was {prob}; original kept at {bad} for the audit trail", "warn")
    else:
        log("no cursor file -- treating this as a first run", "warn")
    log(f"--catch-up {cfg.catch_up}" if cfg.catch_up
        else "--bootstrap: messages between the old cursor and the newest will NOT be ingested",
        "warn")
    return 0


def preserve_bad_cursor():
    """Move an unreadable/corrupt cursor aside instead of overwriting it.

    Overwriting destroys the only evidence of where ingestion actually stopped,
    which is exactly what you want after a suspected message loss.
    """
    p = last_id_path()
    dest = p.with_name(f"{p.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(p, dest)
    except OSError as e:
        log(f"could not move bad cursor aside: {e}", "error")
        return str(dest) + " (MOVE FAILED)"
    return str(dest)


def refuse_no_cursor():
    """Loud, actionable refusal for the silent-loss path. Returns exit code 1."""
    log(f"no usable message cursor: {cursor_problem() or 'absent'}", "error")
    log("REFUSING TO START. With no cursor the poller would baseline at the "
        "newest message, so everything posted while it was down is discarded "
        "-- no inbox row, no warning, no way to recover it later.", "error")
    log("pick one deliberately:", "error")
    log("  --catch-up N   ingest the last N messages into the inbox first, "
        "then baseline (use this if the watcher has been stopped)", "error")
    log("  --bootstrap    accept the gap and start from now; logs one WARN "
        "naming the message it jumped to", "error")
    return 1


def save_last_id(mid):
    _atomic_write(last_id_path(), str(mid))


def iter_inbox(path=None):
    """Yield archived inbox rows, skipping lines that will not parse.

    A torn final line is the expected shape of an append that never finished, so
    it must not be fatal -- this runs at startup to work out what was already
    stored, and dying there would make the watcher unstartable after exactly the
    failure it needs to reason about.
    """
    p = path or inbox_path()
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def read_inbox(path=None, limit=0):
    """Inbox rows, de-duplicated by message id (first occurrence wins).

    Rows are appended before the cursor advances, so a crash in between re-
    delivers the same message and the JSONL ends up holding two copies. Filtering
    on read fixes that, and also neutralises duplicates an earlier crash already
    wrote -- which a writer-side check alone never would.
    """
    seen, out = set(), []
    for row in iter_inbox(path):
        mid = str(row.get("id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(row)
    return out[-limit:] if limit else out


def inbox_ids(path=None):
    """Set of message ids already archived, for the writer-side duplicate check."""
    return {str(r["id"]) for r in iter_inbox(path) if r.get("id")}


def append_inbox(msg, seen=None):
    """Archive one message. Returns False if its id is already stored.

    `seen` (normally state["seen"], seeded from the file at startup) turns a
    re-delivery into a no-op instead of a second row, which keeps history.json
    from replaying the same turn twice to the LLM. The fsync is deliberate: the
    row has to be on disk *before* the cursor that claims it moves, or a power
    cut loses the message while the cursor insists it was handled. Cost is nil
    at a few messages an hour.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    mid = str(msg["id"])
    if seen is None:
        # No cache handed in: scan the file. Safe by default, so a future caller
        # that forgets `seen` still cannot write a duplicate row. The poller does
        # pass state["seen"], seeded from this same file at startup, and skips the
        # re-scan -- without that, the cache would cost a full read per message.
        if mid in inbox_ids():
            return False
    elif mid in seen:
        return False
    _write_inbox_row(msg)
    if seen is not None:
        seen.add(mid)
    return True


def _write_inbox_row(msg):
    row = {"id": msg["id"], "ts": msg.get("timestamp"),
           "author": author_name(msg), "author_id": (msg.get("author") or {}).get("id"),
           "content": msg.get("content", ""), "mentions": [m.get("id") for m in msg.get("mentions", [])]}
    with inbox_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True



def load_history():
    try:
        h = json.loads(history_path().read_text(encoding="utf-8"))
        return h if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def save_history(hist):
    _atomic_write(history_path(), json.dumps(hist[-60:], ensure_ascii=False))


# ---------------------------------------------------------------- LLM
def llm_chat(url, model, messages, max_tokens, temperature, timeout, thinking):
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": temperature, "stream": False}
    if thinking in ("on", "off"):
        payload["chat_template_kwargs"] = {"enable_thinking": thinking == "on"}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions",
                                 method="POST",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"LLM unreachable: {type(e).__name__}: {e}")
    try:
        choice = data["choices"][0]["message"]
    except (KeyError, IndexError):
        raise RuntimeError(f"LLM odd response: {str(data)[:200]}")
    text = (choice.get("content") or "").strip()
    if not text and choice.get("reasoning_content"):
        text = choice["reasoning_content"].strip()
    return text


# ---------------------------------------------------------------- main
def build_prompt(cfg, system, history, batch):
    msgs = [{"role": "system", "content": system}]
    for h in history[-cfg.hist_msgs:]:
        msgs.append({"role": h["role"], "content": h["content"]})
    body = "\n".join(f"[{stamp(m)}] {author_name(m)}: {m.get('content','')}".strip()
                     for m in batch)
    if len(body) > cfg.max_chars:
        body = body[-cfg.max_chars:]
    msgs.append({"role": "user", "content": f"New channel messages:\n{body}\n\nYour reply:"})
    return msgs


def mentioned(msg, bot_id):
    """True if the message targets the bot.

    A real mention object is the reliable signal, but a message posted through a
    webhook with allowed_mentions.parse=[] never gets one even when it contains
    a literal <@id>. Match the raw text too, otherwise agent-to-agent mentions
    over the webhook path are silently ignored.
    """
    if not bot_id:
        return False
    if bot_id in [m.get("id") for m in msg.get("mentions", [])]:
        return True
    return re.search(rf"<@!?{re.escape(bot_id)}>", msg.get("content") or "") is not None


def triggered(msg, cfg, bot_id):
    if cfg.respond_all:
        return True
    if mentioned(msg, bot_id):
        return True
    if msg.get("referenced_message") and is_mine(msg["referenced_message"], bot_id, cfg.wh_id):
        return True
    content = (msg.get("content") or "").lstrip()
    return any(content == p or content.startswith(p + " ") for p in cfg.prefixes)


def poll_once(cfg, api, state):
    if "seen" not in state:            # lazily seeded from what is already archived
        state["seen"] = inbox_ids()
    bootstrap = getattr(cfg, "bootstrap", False)

    if state["last"] is None:
        # main() refuses to let this happen without --catch-up/--bootstrap; the
        # warning stays here too because poll_once is importable on its own.
        msgs, err = api.messages(cfg.channel, limit=1)
        if msgs is None:
            log(f"cannot read channel: {err}", "error")
            return 2
        if not msgs:
            log("channel empty")
            return 0
        seed = msgs[0]["id"]
        ingested = 0
        if cfg.catch_up:
            msgs, err = api.messages(cfg.channel, limit=min(cfg.catch_up, 50))
            if msgs is None:
                log(f"cannot read channel: {err}", "error")
                return 2
            msgs = list(reversed(msgs))
            for m in msgs[:-1]:
                if int(m.get("type", 0)) == 0 and not is_mine(m, cfg.bot_id, cfg.wh_id):
                    if not append_inbox(m, state["seen"]):
                        continue
                    ingested += 1
                    state["history"].append({"role": "user",
                                             "content": f"[{stamp(m)}] {author_name(m)}: {m.get('content','')}"})
        state["last"] = seed
        save_last_id(seed)
        log(f"baseline set at message {seed}"
            + (f", ingested {ingested} older message(s)" if cfg.catch_up else "")
            + (" (--bootstrap)" if bootstrap else "")
            + "; everything older was NOT ingested", "warn")
        return 0

    msgs, err = api.messages(cfg.channel, after=state["last"])
    if msgs is None:
        log(f"poll failed: {err}", "error")
        return 2

    new = sorted((m for m in msgs), key=lambda m: int(m["id"]))
    if not new:
        return 0

    batch = []
    for m in new:
        state["last"] = m["id"]
        if int(m.get("type", 0)) != 0:
            continue        # system message: join, pin, boost, thread stub
        if is_mine(m, cfg.bot_id, cfg.wh_id):
            continue
        if not append_inbox(m, state["seen"]):
            # cursor had not advanced the last time this id was stored
            log(f"msg {m['id']} re-delivered but already archived -- skipped", "warn")
            continue
        if not (m.get("content") or "").strip():
            state["blank"] = state.get("blank", 0) + 1
            if state["blank"] in (1, 10, 50):
                log(f"{state['blank']} empty message(s) from others -- if these "
                    f"are not attachments, Message Content Intent is off "
                    f"(run --diagnose)", "warn")
        line = f"[{stamp(m)}] {author_name(m)}: {m.get('content','')}"
        log(f"<- {author_name(m)}: {m.get('content','')[:160]}")
        state["history"].append({"role": "user", "content": line})
        if triggered(m, cfg, cfg.bot_id):
            batch.append(m)

    save_last_id(state["last"])
    save_history(state["history"])

    if not batch:
        return 0
    if cfg.dry_run or not cfg.answer:
        log(f"{len(batch)} trigger(s), not answering (--dry-run/--no-answer)")
        return 0
    return answer(cfg, state, batch)


def answer(cfg, state, batch):
    if state["replies"] >= cfg.max_replies:
        log(f"reply budget spent ({cfg.max_replies}/hr), skipping", "warn")
        return 0
    system = cfg.system
    if cfg.system_file:
        try:
            system = Path(cfg.system_file).read_text(encoding="utf-8")
        except OSError as e:
            log(f"cannot read --system-file: {e}", "warn")

    prompt = build_prompt(cfg, system, state["history"], batch)
    try:
        reply = llm_chat(cfg.llm_url, cfg.llm_model, prompt, cfg.max_tokens,
                         cfg.temperature, cfg.llm_timeout, cfg.thinking)
    except RuntimeError as e:
        log(str(e), "error")
        return 3

    if not reply or reply.strip() in ("NO_REPLY", "NO_REPLY."):
        log("model stayed silent (NO_REPLY)")
        return 0

    reply = defang(reply)
    for part in chunk(reply):
        if cfg.send_via == "bot" or (cfg.send_via == "auto" and cfg.bot_id):
            s, b = state["api"].send(cfg.channel, part)
            if s in (200, 201):
                log(f"-> posted via bot ({len(part)} chars)")
                continue
            log(f"bot send failed HTTP {s} {b[:120]}, falling back to webhook", "warn")
        if not cfg.webhook:
            log("no webhook configured, dropping reply", "error")
            return 2
        s, b = post_webhook(cfg.webhook, part, cfg.username)
        if s in (200, 201, 204):
            log(f"-> posted via webhook ({len(part)} chars)")
        else:
            log(f"webhook failed HTTP {s} {b[:120]}", "error")
            return 2
        time.sleep(0.6)

    state["history"].append({"role": "assistant", "content": reply})
    save_history(state["history"])
    state["replies"] += 1
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="Discord hive receiver (stdlib REST poller)",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel", default=os.environ.get("HIVE_CHANNEL_ID", "2000000000000000003"))
    p.add_argument("--interval", type=float, default=float(os.environ.get("HIVE_POLL", "3")))
    p.add_argument("--once", action="store_true", help="single poll then exit")
    p.add_argument("--test", action="store_true", help="check auth/access and exit")
    p.add_argument("--dry-run", action="store_true", help="no LLM, no posting")
    p.add_argument("--no-answer", action="store_true", help="inbox only, never reply")
    p.add_argument("--respond-all", action="store_true",
                   help="answer every message, not just mentions")
    p.add_argument("--prefix", action="append", default=[],
                   help="trigger prefix (repeatable); default !hive")
    p.add_argument("--username", default="qwen local agent")
    p.add_argument("--llm-url", default=os.environ.get("HIVE_LLM_URL", "http://127.0.0.1:8004/v1"))
    p.add_argument("--llm-model", default=os.environ.get("HIVE_LLM_MODEL", "qwen3.8-flash-next"))
    p.add_argument("--max-tokens", type=int, default=int(os.environ.get("HIVE_MAX_TOKENS", "400")))
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--llm-timeout", type=float, default=180.0)
    p.add_argument("--thinking", choices=["auto", "on", "off"],
                   default=os.environ.get("HIVE_THINKING", "off"),
                   help="off is the default: llama.cpp `auto` leaks the reasoning trace into content and costs 3-4x the wall time")
    p.add_argument("--system-file", default="", help="persona prompt file")
    p.add_argument("--hist-msgs", type=int, default=20)
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--catch-up", type=int, default=0,
                   help="when there is no cursor, ingest N recent messages into the inbox first")
    p.add_argument("--bootstrap", action="store_true",
                   help="deliberately accept the gap: baseline at the newest message and log a WARN "
                        "(the only alternative to --catch-up when no cursor exists)")
    p.add_argument("--send-via", choices=["auto", "bot", "webhook"], default="auto")
    p.add_argument("--diagnose", action="store_true",
                   help="check whether message content is redacted, then exit")
    p.add_argument("--max-replies", type=int, default=int(os.environ.get("HIVE_MAX_REPLIES", "20")),
                   help="reply budget per rolling hour (loop guard)")
    return p.parse_args()


def diagnose(cfg, api):
    """Detect silent content redaction (missing Message Content Intent).

    Discord blanks `content` for messages the bot did not author when the bot
    lacks Message Content Intent -- in REST responses too, not just gateway
    events. The failure is silent: the poller ingests an empty string, no
    trigger ever matches, and nothing looks broken. Measured here 2026-09-13:
    our own posts come back intact, every foreign message comes back ''.
    """
    msgs, err = api.messages(cfg.channel, limit=25)
    if msgs is None:
        log(f"cannot read channel: {err}", "error")
        return 2
    mine = [m for m in msgs if is_mine(m, cfg.bot_id, cfg.wh_id)]
    theirs = [m for m in msgs if not is_mine(m, cfg.bot_id, cfg.wh_id)]
    log(f"{len(msgs)} message(s): {len(mine)} ours, {len(theirs)} others")
    for m in msgs[:10]:
        c = m.get("content")
        log("  %s type=%s len=%d %r" % (
            "ours " if is_mine(m, cfg.bot_id, cfg.wh_id) else "other",
            m.get("type"), len(c or ""), (c or "")[:50]))

    filled = [m for m in theirs if m.get("content")]
    blank  = [m for m in theirs if not m.get("content")]
    if theirs and not filled and blank:
        log("VERDICT: every foreign message has empty content -- Discord is "
            "redacting it", "error")
        log("fix: Developer Portal -> Bot -> Privileged Gateway Intents -> "
            "enable Message Content Intent -> Save. No re-invite needed.", "error")
        return 4
    if blank and filled:
        log("mixed: some foreign messages are empty, which is normal for "
            "attachment/sticker posts -- not conclusive")
        return 0
    if not theirs:
        log("no foreign messages to judge from -- post something as a human "
            "first")
        return 0
    log("content looks readable")
    return 0


def main():
    cfg = parse_args()
    cfg.answer = not cfg.no_answer
    cfg.prefixes = cfg.prefix or ["!hive"]

    sec = load_secrets()
    cfg.bot_id = sec["app_id"]
    cfg.webhook = sec["webhook"]
    cfg.wh_id = webhook_id_from(cfg.webhook)
    cfg.system = DEFAULT_SYSTEM

    if not sec["token"]:
        log(f"no bot token in {CONF_DIR}/discord", "error")
        return 1

    api = Discord(sec["token"])

    if cfg.test:
        me, err = api.whoami()
        if me is None:
            log(f"auth FAILED: {err}", "error")
            return 2
        log(f"authenticated as {me.get('username')} (id {me.get('id')})")
        msgs, err = api.messages(cfg.channel, limit=1)
        if msgs is None:
            log(f"channel {cfg.channel}: {err}", "error")
            log("the bot is not in the guild yet -- invite it, then re-run --test")
            return 2
        log(f"channel {cfg.channel}: readable, {len(msgs)} message(s) fetched")
        return 0

    if cfg.diagnose:
        return diagnose(cfg, api)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    rc = check_cursor(cfg)
    if rc:
        return rc

    state = {"last": load_last_id(), "history": load_history(),
             "replies": 0, "window_start": time.time(), "api": api}
    log(f"watching channel {cfg.channel} every {cfg.interval}s | "
        f"llm={cfg.llm_model}@{cfg.llm_url} | mode="
        f"{'dry-run' if cfg.dry_run else ('inbox-only' if cfg.no_answer else ('respond-all' if cfg.respond_all else 'mention'))}")

    rc = 0
    while running:
        if time.time() - state["window_start"] > 3600:
            state["window_start"] = time.time()
            state["replies"] = 0
        try:
            rc = poll_once(cfg, api, state)
        except Exception as e:  # keep the loop alive on anything unexpected
            log(f"unexpected: {type(e).__name__}: {e}", "error")
            rc = 2
        if cfg.once:
            break
        for _ in range(int(cfg.interval * 10)):
            if not running:
                break
            time.sleep(0.1)

    return rc


if __name__ == "__main__":
    sys.exit(main())
