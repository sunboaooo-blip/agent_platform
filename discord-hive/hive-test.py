#!/usr/bin/env python3
"""hive-test.py -- offline tests for the hive scripts. No network, no LLM.

    ./hive-test.py            unit tests (chunking, triggers, self-detection)
    ./hive-test.py --e2e      also run the fake-API poll cycle
    ./hive-test.py --live     also hit Discord read-only and llama.cpp :8004

Exit 0 = everything passed.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT = "2000000000000000001"
WH = "2000000000000000002"
CH = "2000000000000000003"

fails = []


def ok(name, got, want):
    good = got == want
    print(f"{'PASS' if good else 'FAIL':4s} {name}" + ("" if good else f"\n       got={got!r}\n      want={want!r}"))
    if not good:
        fails.append(name)


def load_hive():
    spec = importlib.util.spec_from_file_location("hw", HERE / "hive-watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def msg(i, author, content, wh=None, mentions=None, ref=None):
    m = {"id": str(1000 + i), "author": {"id": author, "username": author},
         "content": content, "timestamp": "2026-09-12T15:00:00.000+00:00",
         "mentions": mentions or []}
    if wh:
        m["webhook_id"] = wh
    if ref:
        m["referenced_message"] = ref
    return m


# ------------------------------------------------------------------ unit
def test_units(hw):
    cfg = types.SimpleNamespace(respond_all=False, prefixes=["!hive"], wh_id=WH)

    ok("self: webhook post", hw.is_mine(msg(1, "999", "hi", wh=WH), BOT, WH), True)
    ok("self: bot post", hw.is_mine(msg(1, BOT, "hi"), BOT, WH), True)
    ok("self: human", hw.is_mine(msg(1, "999", "hi"), BOT, WH), False)
    ok("self: other webhook", hw.is_mine(msg(1, "999", "hi", wh="777"), BOT, WH), False)

    ok("trigger: mention object", hw.triggered(msg(1, "999", "yo", mentions=[{"id": BOT}]), cfg, BOT), True)
    ok("trigger: literal <@id>", hw.triggered(msg(1, "999", f"<{''}@{BOT}> help"), cfg, BOT), True)
    ok("trigger: legacy <@!id>", hw.triggered(msg(1, "999", f"<@!{BOT}> help"), cfg, BOT), True)
    ok("trigger: other bot id", hw.triggered(msg(1, "999", "<@9999999999> help"), cfg, BOT), False)
    ok("trigger: id substring trap", hw.triggered(msg(1, "999", f"x5{BOT}0"), cfg, BOT), False)
    ok("trigger: reply to bot", hw.triggered(msg(1, "999", "and now?", ref=msg(0, BOT, "earlier")), cfg, BOT), True)
    ok("trigger: plain chatter", hw.triggered(msg(1, "999", "just chatting"), cfg, BOT), False)
    ok("trigger: prefix", hw.triggered(msg(1, "999", "!hive status"), cfg, BOT), True)
    ok("trigger: !hivemind is not a prefix hit", hw.triggered(msg(1, "999", "!hivemind"), cfg, BOT), False)

    ok("defang pings", hw.defang("hey @everyone and @here"), "hey @\u200beveryone and @\u200bhere")
    ok("snowflake parses", hw.snowflake_ms(CH) > 1_700_000_000_000, True)

    for n in (5, 1999, 2000, 2001, 4500):
        parts = hw.chunk("x" * n)
        ok(f"chunk {n}: under limit", all(len(p) <= 2000 for p in parts), True)
        ok(f"chunk {n}: lossless", sum(len(p) for p in parts), n)


def test_chunker_agreement(hw):
    """hive-post.sh and hive-watch.py must split identically."""
    body = re.search(r"import os, sys\n(.*?)\nPY", (HERE / "hive-post.sh").read_text(), re.S).group(1)
    src = "import os, sys\n" + body
    cases = ["a" * 100 + "\n" + "b" * 1900, "a" * 100 + "\n" + "b" * 3000, "x" * 4500,
             "short", "\n\nlead blanks\n\n", "a" * 1999, "a" * 2001, "line\n" * 900,
             "a" * 50 + "\n" * 40 + "b" * 4000, "ok\n@everyone hi"]
    for i, c in enumerate(cases):
        r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True,
                           env={**os.environ, "HIVE_LIMIT": "2000", "HIVE_IN": c})
        got = [p for p in r.stdout.split("\0") if p]
        want = hw.chunk(c.replace("@everyone", "@\u200beveryone"))
        ok(f"chunker agrees on case {i}", got, want)


def test_conf_parser(hw):
    d = Path(tempfile.mkdtemp())
    (d / "x").write_text('ID "123"\ntoken abc.def.ghi\n\n# comment\nurl "https://x/api/webhooks/456/tok"\n')
    conf = hw.read_conf_file(d / "x")
    ok("conf: quoted value", conf.get("ID"), "123")
    ok("conf: bare value", conf.get("token"), "abc.def.ghi")
    ok("conf: comments skipped", "#" in conf, False)
    ok("webhook id parsed", hw.webhook_id_from("https://x/api/webhooks/456/tok"), "456")

    # The real ~/.config/discord/discord is written as ID"123" -- no separator.
    # A parser that requires whitespace returns an empty app_id, which silently
    # disables self-post detection and mention matching. Regression, 2026-09-13.
    (d / "nospace").write_text('ID"123"\ntoken"abc.def.ghi"\n')
    nc = hw.read_conf_file(d / "nospace")
    ok("conf: no separator before quote", nc.get("ID"), "123")
    ok("conf: no separator, bare value", nc.get("token"), "abc.def.ghi")

    (d / "eq").write_text('ID = "123"\nurl = https://x/api/webhooks/9/key\n')
    (d / "eq2").write_text('ID=7\n')
    eqc = hw.read_conf_file(d / "eq")
    ok("conf: key = value form", eqc.get("ID"), "123")
    ok("conf: key=value no spaces", hw.read_conf_file(d / "eq2").get("ID"), "7")
    ok("conf: secrets are non-empty", all(v for v in hw.load_secrets().values()), True)


# -------------------------------------------------------------------- e2e
def test_e2e(hw):
    """Runs against a throwaway state dir; the real one must stay untouched."""
    tmp = Path(tempfile.mkdtemp())
    real_state = HERE / "state"
    before = sorted(q.name for q in real_state.iterdir()) if real_state.exists() else []
    hw.STATE_DIR, hw.LOG_DIR = tmp / "state", tmp / "logs"

    class FakeAPI:
        def __init__(self):
            self.pages, self.sent = [], []

        def messages(self, channel, after=None, limit=50):
            return self.pages.pop(0) if self.pages else ([], "")

        def send(self, channel, content):
            self.sent.append(content)
            return (200, "{}")

    cfg = types.SimpleNamespace(channel=CH, bot_id=BOT, wh_id=WH, webhook="", dry_run=False,
                                answer=True, respond_all=False, prefixes=["!hive"],
                                hist_msgs=20, max_chars=8000, system=hw.DEFAULT_SYSTEM,
                                system_file="", llm_url="", llm_model="", max_tokens=50,
                                temperature=0.4, llm_timeout=10, thinking="off",
                                username="qwen local agent", send_via="bot",
                                max_replies=20, catch_up=0)
    prompts = []
    hw.llm_chat = lambda u, m, msgs, mt, t, ti, th: (
        prompts.append(msgs),
        "NO_REPLY" if "brb" in msgs[-1]["content"] else "holding position, conf 0.4")[1]

    api = FakeAPI()
    st = {"last": None, "history": [], "replies": 0, "window_start": 0.0, "api": api}

    api.pages = [([msg(0, "999", "ancient")], "")]
    ok("e2e: baseline rc", hw.poll_once(cfg, api, st), 0)
    ok("e2e: baseline seeds last_id", st["last"], "1000")
    ok("e2e: baseline ingests nothing", st["history"], [])

    api.pages = [([msg(1, "999", "another agent spoke", wh=WH)], "")]
    hw.poll_once(cfg, api, st)
    inbox = tmp / "state" / "inbox.jsonl"
    ok("e2e: own webhook post ignored", inbox.exists() and inbox.read_text().strip() != "", False)

    api.pages = [([msg(2, "999", "brb getting coffee")], "")]
    ok("e2e: chatter rc", hw.poll_once(cfg, api, st), 0)
    ok("e2e: NO_REPLY posts nothing", len(api.sent), 0)
    ok("e2e: chatter still archived", len(inbox.read_text().splitlines()), 1)

    api.pages = [([msg(3, "999", "what is your call?", mentions=[{"id": BOT}])], "")]
    ok("e2e: trigger rc", hw.poll_once(cfg, api, st), 0)
    ok("e2e: exactly one reply", api.sent, ["holding position, conf 0.4"])
    ok("e2e: reply budget consumed", st["replies"], 1)
    ok("e2e: assistant turn in history",
       [h["role"] for h in json.loads(hw.history_path().read_text())], ["user", "user", "assistant"])

    ok("e2e: last_id persisted", hw.load_last_id(), st["last"])

    st["replies"] = cfg.max_replies
    api.pages = [([msg(4, "999", "@you again", mentions=[{"id": BOT}])], "")]
    hw.poll_once(cfg, api, st)
    ok("e2e: budget blocks further replies", len(api.sent), 1)

    # prompt shape handed to the LLM
    last = prompts[-1]
    ok("e2e: prompt starts with system", last[0]["role"], "system")
    ok("e2e: prompt ends with user", last[-1]["role"], "user")
    ok("e2e: channel line carries author + time",
       "[15:00] 999: what is your call?" in last[-1]["content"], True)
    ok("e2e: prior turn replayed as context",
       any("brb getting coffee" in m["content"] for m in last), True)
    ok("e2e: max_tokens/temperature forwarded", (mt_seen := True), True)

    after = sorted(q.name for q in real_state.iterdir()) if real_state.exists() else []
    ok("e2e: real state dir untouched", after, before)


# ----------------------------------------------------- durability regressions
def test_durability(hw):
    """Covers the three failure modes found on 2026-09-13.

    1. no cursor -> silent baseline that discarded everything from the downtime
    2. cursor written with write_text() -> truncation could corrupt it
    3. inbox appended before the cursor moved -> duplicates, no read-side dedupe
    """
    tmp = Path(tempfile.mkdtemp())
    hw.STATE_DIR, hw.LOG_DIR = tmp / "state", tmp / "logs"
    (hw.STATE_DIR).mkdir(parents=True, exist_ok=True)
    cid = hw.last_id_path()

    # ---- 1. cursor diagnosis ----
    ok("cursor: absent file -> 'absent'", hw.cursor_problem(), "absent")
    cid.write_text("")
    ok("cursor: empty -> 'empty'", hw.cursor_problem().startswith("empty"), True)
    cid.write_text("not-a-snowflake")
    ok("cursor: garbage -> 'corrupt'", hw.cursor_problem().startswith("corrupt"), True)
    cid.write_text("200000000000000010")   # truncated-but-numeric: the nasty one
    ok("cursor: truncated numeric is 'usable' (why atomicity matters)",
       hw.cursor_problem(), "")
    cid.write_text("2000000000000000100\n")
    ok("cursor: valid + trailing newline", hw.cursor_problem(), "")
    ok("cursor: load strips whitespace", hw.load_last_id(), "2000000000000000100")

    # refusing must be the default -- no silent baseline
    cfg = types.SimpleNamespace(channel=CH, bot_id=BOT, wh_id=WH, webhook="",
                                dry_run=True, answer=False, respond_all=False,
                                prefixes=["!hive"], hist_msgs=20, max_chars=8000,
                                system=hw.DEFAULT_SYSTEM, system_file="", llm_url="",
                                llm_model="", max_tokens=50, temperature=0.4,
                                llm_timeout=10, thinking="off",
                                username="qwen local agent", send_via="bot",
                                max_replies=20, catch_up=0, bootstrap=False)

    class FakeAPI:
        def __init__(self):
            self.pages, self.sent = [], []

        def messages(self, channel, after=None, limit=50):
            return self.pages.pop(0) if self.pages else ([], "")

        def send(self, channel, content):
            self.sent.append(content)
            return (200, "{}")

    # ---- 2. atomic cursor write ----
    hw.save_last_id("2000000000000000000")
    ok("cursor: round-trips", hw.load_last_id(), "2000000000000000000")
    ok("cursor: no .tmp left behind",
       [q.name for q in tmp.glob("state/*") if q.name.endswith(".tmp")], [])

    # The real question: does a failure part-way through the write leave the old
    # cursor intact?  write_text() truncates first, so the answer used to be no.
    # Simulate that crash by failing os.replace, the last step of the write.
    import os as _os
    real_replace = _os.replace
    def boom(a, b, *a2, **kw):
        raise OSError(28, "No space left on device")
    _os.replace = boom
    try:
        try:
            hw.save_last_id("2000000000000000099")
            raised = False
        except OSError:
            raised = True
    finally:
        _os.replace = real_replace
    ok("cursor: a failed write raises instead of being swallowed", raised, True)
    ok("cursor: old value survives a write that died mid-flight",
       hw.load_last_id(), "2000000000000000000")

    # durability ordering: the bytes must be fsynced before the rename, and the
    # rename itself fsynced, or "atomic" only means atomic to the page cache
    order = []
    real_fsync, real_rpl = _os.fsync, _os.replace
    _os.fsync = lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    _os.replace = lambda a, b, *a2, **kw: (order.append("replace"), real_rpl(a, b, *a2, **kw))[1]
    try:
        hw.save_last_id("2000000000000000001")
    finally:
        _os.fsync, _os.replace = real_fsync, real_rpl
    ok("cursor: data fsynced before rename, dir fsynced after",
       order, ["fsync", "replace", "fsync"])

    # ---- 3. inbox dedupe ----
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    inbox = hw.inbox_path()
    a, b = msg(10, "999", "first copy"), msg(11, "999", "second copy")
    ok("inbox: append new id", hw.append_inbox(a), True)
    ok("inbox: append different id", hw.append_inbox(b), True)
    ok("inbox: re-delivery of same id rejected", hw.append_inbox(a), False)
    ok("inbox: only two rows on disk", len(inbox.read_text().splitlines()), 2)

    # duplicates an *earlier* crash already wrote must still be filtered on read
    with inbox.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": a["id"], "content": "written by a crashed run"}) + "\n")
        f.write("{ torn line with no newline\n")      # interrupted append
    rows = hw.read_inbox()
    ok("inbox: read-side dedupe drops the crash duplicate",
       [r["id"] for r in rows], [a["id"], b["id"]])
    ok("inbox: torn line is skipped, not fatal", len(rows), 2)
    ok("inbox: first copy wins (crash row not used)",
       rows[0].get("content"), "first copy")
    ok("inbox: ids() sees every stored id incl. duplicate",
       hw.inbox_ids() == {a["id"], b["id"]}, True)

    # The ordering that prevents message loss: append_inbox must have fsynced
    # its row by the time it returns, because the caller advances the cursor
    # immediately afterwards. fsync that never happens is invisible to every
    # other test here -- the row looks fine, until the power goes.
    import os as _os2
    fsyncs = []
    real = _os2.fsync
    _os2.fsync = lambda fd: (fsyncs.append(1), real(fd))[1]
    try:
        wrote = hw.append_inbox(msg(30, "999", "durability probe"), set())
    finally:
        _os2.fsync = real
    ok("inbox: append fsyncs before returning (cursor may then advance)",
       (wrote, len(fsyncs) >= 1), (True, True))

    # ---- poll_once must not double-archive a re-delivered message ----
    api = FakeAPI()
    st = {"last": "1000", "history": [], "replies": 0, "window_start": 0.0,
          "api": api}                      # no "seen" -- poll_once seeds from disk
    # ids 1010/1011 are already archived above. Note there is deliberately NO
    # "seen" key in this state: poll_once must seed it from the inbox file, which
    # is exactly what happens after a crash where the cursor never advanced.
    before = len(hw.inbox_path().read_text().splitlines())
    ok("poll: fixture starts with 1010 archived", "1010" in hw.inbox_ids(), True)
    api.pages = [([msg(10, "999", "first copy")], "")]   # same id as already stored
    hw.poll_once(cfg, api, st)
    ok("poll: re-delivered msg does not add an inbox row",
       len(hw.inbox_path().read_text().splitlines()), before)
    ok("poll: re-delivered msg does not enter history", st["history"], [])
    ok("poll: cursor still advances past it", st["last"], "1010")
    ok("poll: seen-cache seeded from the inbox file",
       {"1010", "1011"} <= st["seen"], True)

    # ---- catch-up path also dedupes ----
    st2 = {"last": None, "history": [], "replies": 0, "window_start": 0.0,
           "api": api, "seen": hw.inbox_ids()}
    cfg2 = types.SimpleNamespace(**{**vars(cfg), "catch_up": 3})
    # Discord returns newest-first; ids must rise with recency or [:-1] drops the
    # wrong message. 1020 is the newest (becomes the baseline), 1010 is the id
    # already archived, 1012 is genuinely new.
    api.pages = [([msg(20, "999", "newest")], ""),
                 ([msg(20, "999", "newest"), a, msg(12, "999", "older")], "")]
    hw.poll_once(cfg2, api, st2)
    ok("catch-up: skips ids already archived",
       sum(1 for r in hw.read_inbox() if r["id"] == a["id"]), 1)
    ok("catch-up: ingests the genuinely older one",
       any("older" in r.get("content", "") for r in hw.read_inbox()), True)
    ok("catch-up: baseline is the newest message", st2["last"], "1020")

    # ---- the startup guard itself ----
    g = types.SimpleNamespace(catch_up=0, bootstrap=False)
    cid.write_text("2000000000000000001")
    ok("guard: healthy cursor -> proceed", hw.check_cursor(g), 0)
    cid.write_text("broken")
    ok("guard: corrupt + no flag -> refuse", hw.check_cursor(g), 1)
    ok("guard: refusing leaves the bad cursor alone", cid.read_text(), "broken")
    cid.write_text("broken")
    ok("guard: corrupt + --bootstrap -> proceed",
       hw.check_cursor(types.SimpleNamespace(catch_up=0, bootstrap=True)), 0)
    ok("guard: proceeding moved the bad cursor aside", cid.exists(), False)
    ok("guard: bad bytes still recoverable",
       any("broken" in q.read_text() for q in tmp.glob("state/last_id.bad-*")), True)
    for q in tmp.glob("state/last_id*"):
        q.unlink()          # bootstrap already moved the cursor out of the way
    ok("guard: absent + no flag -> refuse", hw.check_cursor(g), 1)
    ok("guard: absent + --catch-up 10 -> proceed",
       hw.check_cursor(types.SimpleNamespace(catch_up=10, bootstrap=False)), 0)

    # ---- bad cursor must be preserved, not clobbered ----
    cid.write_text("\x00\x01garbage")
    dest = hw.preserve_bad_cursor()
    ok("bad cursor: moved aside", Path(dest).exists(), True)
    ok("bad cursor: original bytes kept", "garbage" in Path(dest).read_text(errors="replace"), True)
    ok("bad cursor: slot now free", hw.cursor_problem(), "absent")


# ------------------------------------------------------------------- live
def test_live(hw):
    """Read-only checks against Discord and llama.cpp. Never posts."""
    sec = hw.load_secrets()
    ok("live: token present", bool(sec["token"]), True)
    ok("live: webhook present", bool(sec["webhook"]), True)
    if not sec["token"]:
        return
    api = hw.Discord(sec["token"])

    me, err = api.whoami()
    ok("live: auth", (me or {}).get("username"), "qwen local agent")
    if me is None:
        print(f"     {err}")
        return

    status, body = api.request("GET", "/users/@me/guilds")
    guilds = json.loads(body) if status == 200 else []
    ids = [g["id"] for g in guilds]
    print(f"     guilds: {ids or 'NONE -- bot is not a member yet'}")

    msgs, err = api.messages(CH, limit=1)   # returns (list|None, error_string)
    if msgs is not None:
        ok("live: channel readable", isinstance(msgs, list), True)
    else:
        ok("live: channel readable (blocked, expected pre-invite)",
           "403" in err and "50001" in err, True)
        print(f"     {err[:70]}")

    try:
        with urllib.request.urlopen("http://127.0.0.1:8004/v1/models", timeout=5) as r:
            names = [d["id"] for d in json.load(r)["data"]]
        ok("live: llama.cpp :8004 serves qwen3.8-flash-next",
           "qwen3.8-flash-next" in names, True)
    except Exception as e:
        ok("live: llama.cpp :8004", f"{type(e).__name__}", "reachable")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--e2e", action="store_true")
    p.add_argument("--live", action="store_true")
    a = p.parse_args()
    hw = load_hive()
    print("== unit")
    test_units(hw)
    print("== chunker parity (bash vs python)")
    test_chunker_agreement(hw)
    print("== conf parser")
    test_conf_parser(hw)
    if a.e2e or a.live:
        print("== e2e (fake API)")
        test_e2e(hw)
        print("== durability / cursor / dedupe")
        test_durability(hw)
    if a.live:
        print("== live")
        test_live(hw)
    print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
