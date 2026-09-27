#!/usr/bin/env python3
"""
ask.py — SSH channel: ask the overnight agent a question from the terminal.

Usage:
  ask.py "what is the current view on NVDA?"
  ask.py --file question.md
  ask.py --list          # show recent conversation history

Works under Qwen (local 127.0.0.1:8000). Recommend-only.
"""
import argparse, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conv_core

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--file", help="read question from file")
    ap.add_argument("--list", action="store_true", help="show recent history")
    ap.add_argument("--clear", action="store_true", help="clear conversation history")
    args = ap.parse_args()

    if args.clear:
        if os.path.exists(conv_core.HIST_PATH):
            os.remove(conv_core.HIST_PATH)
        print("conversation history cleared")
        return
    if args.list:
        hist = conv_core.load_history()[-12:]
        if not hist:
            print("(no conversation yet)")
        for h in hist:
            tag = "YOU  " if h["role"] == "user" else "AGENT"
            print(f"--- {h['ts']} {tag} ---")
            print(h["content"][:800])
        return

    if args.file:
        with open(args.file) as f:
            q = f.read()
    elif args.question:
        q = " ".join(args.question)
    else:
        ap.error("provide a question (args or --file)")
    if not q.strip():
        ap.error("empty question")

    print(f"asking the overnight agent (Qwen 27B local)...\n")
    reply = conv_core.answer(q, meta={"channel": "ssh"})
    print(reply)

if __name__ == "__main__":
    main()
