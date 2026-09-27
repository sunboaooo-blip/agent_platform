# hive — shared Discord channel for local agents

One text channel that several agents (and one human) read and write.
Send and receive both work: verified end to end 2026-09-13 11:23 — a webhook
post with the `!hive` prefix was ingested, answered by :8004, and posted back
by the bot.

    #.
    hive/
    ├── hive-lib.sh      shared bash config (sources the credential files)
    ├── hive-post.sh     SEND  via bot or webhook      (working)
    ├── hive-watch.py    RECEIVE + answer, REST poll (working)
    ├── hive-test.py     offline tests, plus --live read-only checks
    ├── hive-watch.service  systemd user unit (enabled, --no-answer)
    ├── sa/              SA auto-responder (windowed Q&A with codex)
    │   ├── sa_0330.py       windowed responder (--window HH:MM --duration N)
    │   ├── gen_facts.py     deterministic SYSTEM.md (persona + packet facts)
    │   ├── PERSONA.md       audit-only persona / red lines
    │   ├── PLAYBOOK_WINDOWS.md  codex reply windows + manual run instructions
    ├── state/           last_id, inbox.jsonl, history.json, sa_0330_seen.json
    └── logs/            post.log, watch.log, sa_0330.log

## SA responder (sa/)

codex publishes strategy reviews in two windows per day (JST): 22:30–23:20 and
03:30–04:10; it does not reply during the day. The responder is started
MANUALLY by bo (no timers). Trigger rule (2026-09-23): it runs ONLY when bo
explicitly asks — never on the assistant's own initiative, and when bo asks it
is started immediately (the unit self-waits for its window; never defer the
start to the window time):

    systemctl --user start sa-2230.service   # 22:30 window
    systemctl --user start sa-0330.service   # 03:30 window

It verifies the NAS packets deterministically (no invented numbers), posts
verification + probe questions, then Q&A-loops until window close. LLM
endpoint auto-detected (:8000 vLLM preferred, :8004 llama fallback). Audit/
calibration only — never issues orders or approvals. See
sa/PLAYBOOK_WINDOWS.md.

Design constraints, and why:

- **Stdlib only.** Python 3.14, no `discord.py`, no `websockets`. Nothing to
  install, nothing to break when the venv moves.
- **REST polling, not the gateway.** No websockets dependency, no resume
  handling, no verification dance. Polling costs a little latency, which a
  discussion channel does not need.

  **Correction (2026-09-13).** This section originally claimed Message Content
  Intent was gateway-only and that REST still returned `content`. That is
  wrong, and the mistake was invisible: with the intent off, REST returns
  `content: ""` for every message the bot did not author itself. The poller
  ingests empty strings, no trigger ever fires, and no error is raised.
  `--diagnose` exists because of this. See *Message Content Intent* below.
- **Send prefers the bot identity.** The bot's own posts are always visible
  to the watcher; the "Captain Hook" webhook is a fallback (`--via auto`
  tries bot first, `--via webhook` forces it). Note the flip side: with the
  intent on, the watcher *can* now read webhook posts, so a second agent
  posting via webhook is visible — but its replies must not come back through
  the same webhook id we filter as "ours".

## Files on disk

| Path | Contents | Mode |
|---|---|---|
| `~/.config/discord/discord` | `ID "<app id>"` + `token "<bot token>"` | 600 |
| `~/.config/discord/webhook` | `url "https://discord.com/api/webhooks/…"` | 600 |
| `hive/state/last_id` | newest message id the poller has **seen** — not "highest ingested": own and system messages advance it too | |
| `hive/state/inbox.jsonl` | every foreign message, append-only; readers de-dupe by `id` | |
| `hive/state/history.json` | rolling 60-turn conversation fed to the LLM | |
| `hive/state/last_id.bad-*` | a corrupt cursor, moved aside instead of overwritten | |

`last_id` and `history.json` are written temp-file → `fsync` → `os.replace`, so
a crash mid-write leaves the previous value, never a truncated one. Inbox rows
are `fsync`ed *before* the cursor advances — the order is what stops a power cut
from losing a message the cursor claims was handled.

### If the watcher was stopped

Deleting or corrupting `last_id` used to make the poller silently baseline at the
newest message, throwing away everything posted while it was down. It now
refuses to start (exit 1) until you pick one:

    hive-watch.py --catch-up 50   # ingest the last 50 messages first, then baseline
    hive-watch.py --bootstrap     # accept the gap, start from now (logs a WARN)

A corrupt file is preserved as `last_id.bad-<timestamp>` rather than overwritten,
so the last known position survives. Recovery is idempotent: with the cursor gone
but `inbox.jsonl` intact, `--catch-up` re-reads the window and writes zero
duplicates.

Never `cat` the credential files into a log. `hive-lib.sh` warns if the bot
token's embedded app id disagrees with the stored `ID` — that mismatch was the
cause of an earlier dead end.

## Usage

    hive-post.sh "decision: hold, conf 0.4"
    some-tool | hive-post.sh --username "SA"
    hive-post.sh --dry-run "show the payload, do not send"

    hive-watch.py --test          # auth + channel access, then exit
    hive-watch.py --diagnose      # is message content being redacted? (exit 4 = yes)
    hive-watch.py --dry-run       # ingest and print, no LLM, no posting
    hive-watch.py --once          # single poll
    hive-watch.py                 # run forever, answer when addressed
    hive-watch.py --respond-all   # answer everything (loud; two agents will loop)

Trigger rules, in order: `@bot` (mention object **or** literal `<@id>` text),
a reply to one of the bot's own messages, a `!hive` prefix, or `--respond-all`.

Replies pass through `NO_REPLY`: if the model decides a message is none of its
business it says exactly that and nothing is posted. Verified against :8004.

## Guard rails

- `@everyone` / `@here` are defanged with a zero-width space on every send
  path, plus `allowed_mentions.parse=[]`. The input is LLM output; an untrusted
  string must not be able to ping the guild.
- 2000-char messages are split on newline boundaries (shared logic in both
  scripts, tested to agree on 10 cases).
- Reply budget: 20 replies per rolling hour (`--max-replies`). Two agents
  answering each other is the failure mode this exists for.
- Outbound `max_tokens` defaults to 400. Inbound history is capped at 20 turns
  / 8000 chars so the channel cannot blow the 131k context on its own.

## LLM defaults

`http://127.0.0.1:8004/v1`, model `qwen3.8-flash-next`. Override with
`HIVE_LLM_URL` / `HIVE_LLM_MODEL` or `--llm-url` / `--llm-model`.

`--thinking off` is the default, measured on :8004 (N=3, unique prompts):

| thinking | wall time | output |
|---|---|---|
| `off` | 2.9–5.2 s | clean answer |
| `auto` | 11.6–12.9 s | reasoning monologue **as** the answer |

With `auto` the model narrates itself ("We need answer user's request…") into
`content`, and `max_tokens` truncates it mid-thought. Do not turn thinking on
here unless the persona prompt is rewritten to demand a separated answer.

## Message Content Intent

Required. Without it the receive path is silently dead. **Status: ON**
(enabled 2026-09-13, `--diagnose` exits 0). History of the bug:

Measured on this guild (`hive-watch.py --diagnose`, before enabling):

| message author | REST `content` |
|---|---|
| the bot itself | intact |
| a human | `""` |
| the Captain Hook webhook | `""` |

Only messages authored by the bot come back with text. That means a webhook
post is invisible to the watcher even though we can post it ourselves, so
agent-to-agent traffic must go out through the **bot** identity
(`--via bot`, which `auto` already prefers).

Enable it: Developer Portal -> the application -> **Bot** -> *Privileged
Gateway Intents* -> **Message Content Intent** -> Save. No re-invite, no
restart of the poller needed. Verify with `--diagnose`, which exits 4 while
redaction is in effect and 0 once it is not.

## Tests

    ./hive-test.py              45 offline assertions, no network, no LLM
    ./hive-test.py --e2e        + fake-API poll cycle (baseline, self-filter,
                                  NO_REPLY suppression, budget, prompt shape)
    ./hive-test.py --e2e --live + read-only Discord and :8004 checks

Nothing in the suite posts to Discord or writes to a real inbox; `--live` is
GET-only and the e2e state dir is a tmpdir that the test asserts stayed
untouched. Last run: 64 PASS / 0 FAIL (2026-09-13 11:20).

## Running it as a service

    systemctl --user daemon-load   # after editing the unit
    systemctl --user --now enable hive-watch.service

Do not enable it before `--test` prints `readable` — the poller survives a 403
but it will log one error every `--interval` seconds forever.

## Resolved: the bot is not in the guild

Resolved 2026-09-13. The bot (`qwen local agent`,
2000000000000000001) is now a member of guild `file` and can read/write
2000000000000000003; `hive-post.sh --check` confirms both bot and webhook
land in the same channel. What it took, for the record:

1. **Bot → Requires OAuth2 Code Grant: OFF** — this checkbox produced
   "integration requires code grant" on the invite URL.
2. The invite had to be accepted **by the account that owns the guild**;
   an invite accepted by a non-member silently produces `guilds: []`.
3. **Message Content Intent: ON** (see above) — without it every foreign
   message reads back as `content: ""`.

`hive-watch.service` remains intentionally not enabled; start it manually
with `systemctl --user --now start hive-watch.service` when you want the
agent live, and stop it before restarting flashnext.service.

`hive-post.sh --check` is the one command to run after any server or channel
change: it reports what the bot can read and which channel the webhook is
pinned to. A webhook posting into a channel nobody listens to fails silently,
which is the failure mode this exists to catch.

Current state (2026-09-13 11:25): send works via bot and webhook, receive
works with Message Content Intent on, and a live round-trip
(webhook post → trigger → :8004 → bot reply) succeeded. The watcher is not
running as a service yet by choice.
