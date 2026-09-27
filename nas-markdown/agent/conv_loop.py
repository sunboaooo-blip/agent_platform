#!/usr/bin/env python3
"""
conv_loop.py — NAS conversation channel (wake-up without SSH).

The operator (or the mini PC) drops a .md/.txt/.json file into
  NAS /home/sba/ai_pipeline/conversation/inbox/
The agent answers into
  NAS /home/sba/ai_pipeline/conversation/outbox/<same name>.reply.md
and archives both to conversation/history/.

Modes: --once (single pass) / --daemon (systemd)
"""
import os, sys, time, argparse, json
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conv_core
from decision_loop import NAS, log

JST = ZoneInfo("Asia/Tokyo")
NAS_CONV = "/home/sba/ai_pipeline/conversation"
POLL_SECONDS = 30

def run_pass(nas, seen):
    names = nas.listdir(f"{NAS_CONV}/inbox")
    if names is None:
        log("conv: inbox listdir failed (NAS down?)")
        return
    for name in [n for n in names if not n.startswith(".") and os.path.splitext(n)[1] in (".md", ".txt", ".json")]:
        if name in seen:
            continue
        raw = nas.get(f"{NAS_CONV}/inbox/{name}")
        if raw is None:
            continue
        text = raw.decode("utf-8", errors="replace")[:20000]
        log(f"conv: processing {name} ({len(raw)} bytes)")
        reply = conv_core.answer(text, meta={"channel": "nas", "file": name})
        base = os.path.splitext(name)[0]
        ts = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
        out_name = f"{ts}_{base}.reply.md"
        body = f"# Reply to {name}\n\n{reply}\n\n---\n_agent: qwen3.8-27b on gpu-node · recommend-only, execution stays on the control plane_\n"
        ok = nas.put(f"{NAS_CONV}/outbox/{out_name}", body)
        log(f"conv: wrote {out_name} -> {ok}")
        # archive + move source
        nas.put(f"{NAS_CONV}/history/{ts}_{name}.user", raw)
        nas.put(f"{NAS_CONV}/history/{out_name}", body.encode())
        nas.put(f"{NAS_CONV}/inbox/processed/{name}", raw)
        seen.add(name)
        time.sleep(3)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    args = ap.parse_args()
    log(f"conv_loop start (once={args.once})")
    nas = NAS()
    for p in (f"{NAS_CONV}/inbox/processed", f"{NAS_CONV}/outbox", f"{NAS_CONV}/history"):
        nas.mkcol(p)
    seen = set()
    while True:
        try:
            run_pass(nas, seen)
        except Exception as e:
            log(f"conv pass error: {e}")
        if args.once:
            return
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
