# SA responder — codex reply windows (bo remit 2026-09-22)

## When codex replies
codex (both `codex-tiger` and `codex-longbridge`) publishes strategy reviews in
TWO windows per day, JST:
  1. 22:30 – 23:20
  2. 03:30 – 04:10
It will NOT reply during the day. Do not expect daytime Q&A; do not burn
LLM calls polling outside these windows.

## How the responder runs (MANUAL — bo starts it on demand)
- Script: `~/llm-node/hive/sa/sa_0330.py`
- Windowed: `--window HH:MM --duration MIN` (default 50). It self-waits until
  the window opens, verifies the packets, posts verification + probe questions,
  then Q&A-loops until window close. Catch-up pass replies to any late post.
- LLM endpoint auto-detected: vLLM `:8000/qwen3.8-27b` preferred,
  llama `:8004/qwen3.8-flash-next` fallback; re-detects on a failed call.
  If NO endpoint is healthy it aborts loudly (exit 3) — never a silent no-op.
- systemd user units (oneshot, timers DISABLED by bo's choice):
    systemctl --user start sa-0330.service   # 03:30 window
    systemctl --user start sa-2230.service   # 22:30 window
  Or run directly:
    python3 ~/llm-node/hive/sa/sa_0330.py --window 03:30 --duration 50
    python3 ~/llm-node/hive/sa/sa_0330.py --window 22:30 --duration 50
- Log: `~/llm-node/hive/logs/sa_0330.log`
- Seen-state: `~/llm-node/hive/state/sa_0330_seen.json` (persists across runs;
  a codex post is answered at most once).
- If codex hasn't posted by window start: posts verification block only, then
  waits for the post inside the window.

## Trigger rule (bo remit 2026-09-23)
The service is triggered ONLY when bo explicitly asks. Two parts, both
mandatory:

1. **No unasked starts.** Never start sa-2230/sa-0330 on your own initiative —
   not "for safety", not "just in case", not as a follow-up to a question.
   No timers, no cron, no auto-restart. If bo says a window will not run,
   make sure nothing is queued (stop any running instance, confirm dead).
2. **When bo asks, start IMMEDIATELY — do not wait for the window time.**
   The service self-waits until its window opens, so there is no reason to
   defer. If bo asks at 20:00 for the 03:30 window, start it at 20:00 and
   confirm it is armed. Never reply "I'll start it at 03:30" or leave the
   start pending on a question — that is exactly how the 2026-09-23 03:30
   window was missed.

Before starting, confirm an LLM endpoint is up
(`curl -s 127.0.0.1:8000/v1/models` / `:8004`). If neither is up, tell bo and
ask whether to start one (vllm-qwen.service / flashnext) — do not switch stacks
on your own.

## Red lines (unchanged, see PERSONA.md)
Audit/calibration only. No decision commands, no order specs, no approvals,
no execution. Numbers only from VERIFIED packet fields or the message being
replied to.
