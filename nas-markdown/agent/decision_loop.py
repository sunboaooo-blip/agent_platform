#!/usr/bin/env python3
"""
decision_loop.py — overnight decision agent for the GPU node.

Reads agent output from the NAS inbox (mini PC -> NAS -> here), analyzes it
with the local Qwen 27B, and writes structured DECISIONS to the NAS
decisions/ folder for the control plane to consume.

RED LINE (CLAUDE.md): this node RECOMMENDS. It never executes trades.
The control plane owns execution, risk checks and human confirmation.

Modes:
  --once        single pass, then exit (for testing / manual runs)
  --daemon      loop forever (systemd unit runs this)
  --dry-run     analyze but do NOT write to NAS (log only)

Safety gates:
  - Market hours only (NASDAQ regular session, JST)
  - Confidence gate: decisions below CONF_MIN are downgraded to "monitor"
  - Strict JSON output; malformed output -> logged, item kept for retry
  - Max items per pass (protects the 161K-token shared KV pool)

Stdlib only (no openai package needed) — talks to vLLM over raw HTTP.
"""
import base64, json, os, re, sys, time, argparse, urllib.request, urllib.error, urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ET  = ZoneInfo("America/New_York")

HERE = os.path.dirname(os.path.abspath(__file__))
NAS_CFG = os.path.expanduser("~/nas-upload/config.json")
CTX_DIR = os.path.join(HERE, "context")
LOCAL_CONTEXT = os.path.join(CTX_DIR, "portfolio.md")
LOG_PATH = os.path.join(HERE, "logs", "decision_loop.log")
STATE_PATH = os.path.join(HERE, "state.json")

LLM_URL = "http://127.0.0.1:8000/v1"
MODEL = "qwen3.8-27b"

# NAS paths (control plane writes to inbox, reads decisions)
NAS_INBOX = "/home/sba/ai_pipeline/inbox"
NAS_DECISIONS = "/home/sba/ai_pipeline/decisions"
NAS_ALERTS = "/home/sba/ai_pipeline/alerts"
NAS_CONTEXT = "/home/sba/ai_pipeline/context"   # control plane pushes portfolio.md here

CONF_MIN = 0.7          # below this -> "monitor", not "act"
MAX_ITEMS_PER_PASS = 5  # protects shared KV pool
POLL_SECONDS = 60
MAX_ITEM_BYTES = 24000 # ~20K tokens cap per item

# ---------------------------------------------------------------- logging
def log(msg):
    line = f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

# ---------------------------------------------------------------- NAS (WebDAV, stdlib only)
class NAS:
    def __init__(self):
        with open(NAS_CFG) as f:
            cfg = json.load(f)
        self.base = f"http://{cfg['host']}:{cfg['port']}"
        self.auth = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
        self.timeout = cfg.get("timeout", 60)

    def _req(self, method, path, data=None, headers=None):
        url = self.base + urllib.parse.quote(path, safe="/")
        h = {"Authorization": self.auth}
        if headers: h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 0, str(e).encode()

    def mkcol(self, path):
        s, _ = self._req("MKCOL", path)
        return s in (201, 405)  # 405 = already exists

    def listdir(self, path):
        s, body = self._req("PROPFIND", path, headers={"Depth": "1"})
        if s != 207:
            return None
        names = re.findall(rb'<[Dd]:href>([^<]+)</[Dd]:href>', body)
        out = []
        for n in names:
            u = urllib.parse.unquote(n.decode())
            if u == path.rstrip("/") + "/":
                continue
            name = u.split("/")[-1]
            if name:  # skip empty self-reference
                out.append(name)
        return out

    def get(self, path):
        s, body = self._req("GET", path)
        return body if s == 200 else None

    def put(self, path, data):
        if isinstance(data, str): data = data.encode()
        s, _ = self._req("PUT", path, data=data)
        return s in (200, 201)

    def delete(self, path):
        s, _ = self._req("DELETE", path)
        return s in (200, 204)

# ---------------------------------------------------------------- market hours
def nasdaq_session(now_jst):
    """Return (in_session, phase). Phases: pre / open / closed."""
    now_et = now_jst.astimezone(ET)
    if now_et.weekday() >= 5:  # Sat/Sun
        return False, "closed-weekend"
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    pre_t = now_et.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_et < pre_t:
        return False, "closed"
    if now_et < open_t:
        return True, "pre"
    if now_et <= close_t:
        return True, "open"
    return False, "closed"

# ---------------------------------------------------------------- LLM
def llm_health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False

def llm_decide(item_name, item_text, portfolio_ctx, phase):
    """Call Qwen with strict-JSON decision prompt. Returns parsed dict or None."""
    prompt = f"""You are the overnight decision agent for a trading portfolio.
The NASDAQ session is currently in phase: {phase}.
You receive output from a portfolio-monitoring agent that runs on another machine.

HARD RULES:
1. You RECOMMEND. You never execute. Output is consumed by a control plane
   that owns execution, risk limits and human confirmation.
2. Base decisions ONLY on the provided data. If data is insufficient, stale,
   or contradictory, say so and recommend "hold".
3. Prefer "hold" when uncertain. A missed move costs less than a wrong trade.
4. Output STRICT JSON only. No markdown, no commentary outside the JSON.

PORTFOLIO CONTEXT:
{portfolio_ctx}

AGENT OUTPUT (file: {item_name}):
{item_text}

Respond with exactly this JSON schema:
{{
  "action": "act" | "hold" | "monitor",
  "confidence": <float 0..1>,
  "positions": [
    {{"symbol": str, "direction": "buy"|"sell"|"hold"|"reduce", "size_pct": <float, % of position to adjust>, "rationale": str}}
  ],
  "risk_flags": [str],
  "summary": str
}}
"""
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 2048,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": prompt}],
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
        log(f"  LLM request failed: {e}")
        return None
    try:
        choice = reply["choices"][0]
        content = (choice.get("message", {}).get("content") or "").strip()
        if not content:
            log(f"  LLM returned empty content (finish_reason={choice.get('finish_reason')})")
            return None
    except (KeyError, IndexError) as e:
        log(f"  LLM reply malformed: {e}")
        return None
    # strip possible code fences
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
    try:
        d = json.loads(content)
    except json.JSONDecodeError as e:
        log(f"  LLM output not valid JSON: {e}; raw: {content[:300]}")
        return None
    # normalize + gate
    d.setdefault("positions", [])
    d.setdefault("risk_flags", [])
    try:
        d["confidence"] = float(d.get("confidence", 0))
    except (TypeError, ValueError):
        d["confidence"] = 0.0
    if d["action"] == "act" and d["confidence"] < CONF_MIN:
        d["action"] = "monitor"
        d["risk_flags"].append(f"confidence {d['confidence']:.2f} below gate {CONF_MIN}; downgraded act->monitor")
    return d

# ---------------------------------------------------------------- main pass
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"processed": {}}

def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=2)

def portfolio_context():
    p = os.path.join(CTX_DIR, "portfolio.md")
    if os.path.exists(p):
        with open(p) as f:
            return f.read()[:30000]  # cap: KV pool is shared
    return "(no portfolio context file yet — ~/decision-agent/context/portfolio.md is empty/missing; treat as insufficient data and prefer hold)"

def sync_context(nas):
    """Pull portfolio context pushed by the control plane (mini PC) from the NAS.
    The NAS copy is the source of truth; local is a mirror refreshed every pass."""
    raw = nas.get(f"{NAS_CONTEXT}/portfolio.md")
    if raw is None:
        return
    content = raw.decode("utf-8", errors="replace")
    if os.path.exists(LOCAL_CONTEXT):
        with open(LOCAL_CONTEXT) as f:
            if f.read() == content:
                return
    with open(LOCAL_CONTEXT, "w") as f:
        f.write(content)
    log(f"portfolio context synced from NAS ({len(content)} bytes)")

def process_item(nas, name, state, dry_run, phase):
    raw = nas.get(f"{NAS_INBOX}/{name}")
    if raw is None:
        log(f"  GET failed for {name}; skipping this pass")
        return
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_ITEM_BYTES:
        text = text[:MAX_ITEM_BYTES] + "\n...[truncated]"
    log(f"  analyzing {name} ({len(raw)} bytes)")
    decision = llm_decide(name, text, portfolio_context(), phase)
    if decision is None:
        log(f"  NO DECISION for {name} (will retry next pass)")
        return
    ts = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    base = os.path.splitext(name)[0]  # strip source ext to avoid .json.json
    out_name = f"{ts}_{base}.json"
    out = json.dumps({
        "ts": datetime.now(JST).isoformat(),
        "source_item": name,
        "phase": phase,
        "model": MODEL,
        "decision": decision,
        "red_line": "RECOMMENDATION ONLY — execution, risk checks and human confirmation happen on the control plane",
    }, indent=2, ensure_ascii=False)
    if dry_run:
        log(f"  DRY-RUN decision: {json.dumps(decision, ensure_ascii=False)[:500]}")
    else:
        ok = nas.put(f"{NAS_DECISIONS}/{out_name}", out)
        log(f"  wrote {out_name} to NAS: {ok}")
        if ok:
            # move source item out of inbox so it is not re-processed
            nas.put(f"{NAS_INBOX}/processed/{name}", raw)
    state["processed"][name] = ts

def alert(nas, kind, msg):
    ts = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    try:
        nas.put(f"{NAS_ALERTS}/{ts}_{kind}.txt", f"[{datetime.now(JST).isoformat()}] {kind}: {msg}")
        log(f"ALERT({kind}) pushed to NAS: {msg}")
    except Exception as e:
        log(f"ALERT({kind}) local only (NAS unreachable?): {msg} ({e})")

def run_pass(nas, state, dry_run, phase):
    names = nas.listdir(NAS_INBOX)
    if names is None:
        alert(nas, "nas_unreachable", "inbox listdir failed — NAS link down?")
        return
    names = [n for n in names if not n.startswith(".") and os.path.splitext(n)[1] in (".json", ".md", ".txt", ".yaml", ".yml", ".csv")]
    pending = [n for n in names if n not in state["processed"]]
    if not pending:
        return
    for name in pending[:MAX_ITEMS_PER_PASS]:
        try:
            process_item(nas, name, state, dry_run, phase)
        except Exception as e:
            log(f"  error processing {name}: {e}")
        save_state(state)
        time.sleep(5)  # be gentle with the KV pool

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ignore-hours", action="store_true", help="run outside market hours (testing)")
    args = ap.parse_args()

    log(f"decision_loop start (once={args.once}, dry_run={args.dry_run}, ignore_hours={args.ignore_hours})")
    nas = NAS()
    if not args.dry_run:
        for p in (NAS_INBOX, f"{NAS_INBOX}/processed", NAS_DECISIONS, NAS_ALERTS, NAS_CONTEXT):
            nas.mkcol(p)

    state = load_state()
    last_llm_check = 0
    while True:
        now = datetime.now(JST)
        in_session, phase = nasdaq_session(now)
        if not in_session and not args.ignore_hours:
            if args.once:
                log(f"outside NASDAQ session ({phase}); nothing to do")
                return
            time.sleep(300)
            continue
        # LLM watchdog (every 5 min)
        if time.time() - last_llm_check > 300:
            last_llm_check = time.time()
            if not llm_health():
                alert(nas, "llm_down", "local Qwen endpoint 127.0.0.1:8000 not healthy — decisions paused; start with: llm-switch vllm")
            else:
                log("LLM health OK")
        try:
            sync_context(nas)
        except Exception as e:
            log(f"context sync error: {e}")
        try:
            run_pass(nas, state, args.dry_run, phase)
        except Exception as e:
            log(f"pass error: {e}")
        save_state(state)
        if args.once:
            return
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
