#!/usr/bin/env python3
"""SA auto-responder (scoped, one-shot, bo-authorized 2026-09-22).

Bo remit: do NOT issue decision commands / order specs. Verify the packets,
then INTERROGATE the strategy around the core question (raise semi exposure),
grounded in the VERIFIED workflow A/B outputs (B scores, A news) and the
LATEST verified holdings snapshot. Deterministic verification + context are
built in Python; the LLM (as SA) writes only the probe questions, Q&A replies
and the final conclusion report.
"""
import json, hashlib, sys, time, os, urllib.request, datetime

sys.path.insert(0, os.environ.get('SA_WF_DIR', os.path.expanduser('~/wf')))
from nas import get, propfind  # noqa: E402

SA = os.environ.get('SA_DIR', os.path.expanduser('~/llm-node/hive/sa'))
HIVE = os.environ.get('HIVE_DIR', os.path.expanduser('~/llm-node/hive'))
INBOX = f'{HIVE}/state/inbox.jsonl'
SEEN = f'{HIVE}/state/sa_0330_seen.json'
LOG = f'{HIVE}/logs/sa_0330.log'
LLM_URL = None
LLM_MODEL = None

def detect_llm():
    """First healthy endpoint wins: vLLM :8000 (27B) preferred, llama :8004 fallback."""
    for base, pref in (('http://127.0.0.1:8000', 'qwen3.8-27b'),
                       ('http://127.0.0.1:8004', None)):
        try:
            req = urllib.request.Request(base + '/v1/models')
            j = json.loads(urllib.request.urlopen(req, timeout=5).read())
            ids = [m['id'] for m in j.get('data', [])]
            if not ids:
                continue
            return base + '/v1/chat/completions', (pref if pref in ids else ids[0])
        except Exception:
            continue
    return None, None
BOT_ID = '2000000000000000001'
CHANNEL = '2000000000000000003'
CODEX_IDS = {'2000000000000000011', '2000000000000000012', '2000000000000000013'}
MAX_CALLS = 60
POLL = 10
SEMI = {'SOXX','DRAM','INTC','NVDA','TSM','AVGO','MU','SNDK','CSTM','CRDO','TXN','VRT',
        'AMD','TTMI','MRVL','ON','QCOM','KLAC','LRCX','AMAT','ARM','SMCI','WDC','STX',
        'MPWR','ALAB','AAOI'}

def log(msg):
    line = f'[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')

def sha(b): return hashlib.sha256(b).hexdigest()

def load_bot():
    tok = chan = None
    for line in open(os.path.expanduser('~/.config/discord/discord')):
        line = line.strip()
        if line.startswith('token'):
            tok = line.split('token', 1)[1].strip().strip('"').strip("'")
        if line.startswith('ID'):
            chan = line.split('ID', 1)[1].strip().strip('"').strip("'")
    return tok, chan

def post(content, dry=False):
    content = content[:5000]
    chunks, cur = [], ''
    for para in content.split('\n'):
        if len(cur) + len(para) + 1 > 1900:
            chunks.append(cur); cur = para
        else:
            cur = cur + '\n' + para if cur else para
    if cur: chunks.append(cur)
    for ch in chunks:
        ch = ch.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
        if dry:
            log(f'[dry] would post {len(ch)}c'); continue
        tok, _ = load_bot()
        req = urllib.request.Request(f'https://discord.com/api/v10/channels/{CHANNEL}/messages',
            data=json.dumps({'content': ch[:2000], 'allowed_mentions': {'parse': []}}).encode(),
            headers={'Authorization': f'Bot {tok}', 'Content-Type': 'application/json',
                     'User-Agent': 'SA-0330/1.0'}, method='POST')
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f'posted {r.status} {len(ch)}c')

def llm(messages, max_tokens=900, dry=False):
    global LLM_URL, LLM_MODEL
    if dry:
        return '[dry-run: no LLM call]'
    payload = {'model': LLM_MODEL, 'messages': messages, 'max_tokens': max_tokens,
               'temperature': 0.3, 'chat_template_kwargs': {'enable_thinking': False}}
    for attempt in range(2):
        try:
            req = urllib.request.Request(LLM_URL, data=json.dumps(payload).encode(),
                headers={'Content-Type': 'application/json', 'User-Agent': 'SA-0330/1.0'}, method='POST')
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.loads(r.read())
            break
        except Exception as e:
            if attempt == 0:
                log(f'LLM call failed ({e}); re-detecting endpoint')
                LLM_URL, LLM_MODEL = detect_llm()
                if not LLM_URL:
                    raise
                payload['model'] = LLM_MODEL
            else:
                raise
    txt = j['choices'][0]['message'].get('content', '').strip()
    if txt.startswith('```'):
        txt = txt.strip('`')
        if txt.startswith('text'): txt = txt[4:].strip()
    return txt

def read_inbox():
    rows = []
    with open(INBOX, 'rb') as f:
        for l in f.read().decode('utf-8', errors='replace').splitlines():
            try:
                m = json.loads(l)
                if int(m['id']) > 0:
                    rows.append(m)
            except Exception:
                pass
    rows.sort(key=lambda m: int(m['id']))
    return rows

def load_seen():
    try: return set(json.load(open(SEEN)))
    except Exception: return set()

def save_seen(s):
    with open(SEEN, 'w') as f: json.dump(sorted(s), f)

# ---- deterministic verification ----
def verify_packets():
    """Deterministic checks. Returns (full, compact): full for the log,
    compact for the Discord post / LLM context (no hashes/paths)."""
    full, compact = [], []
    B = '/ai_pipeline/trade_data/workflow_b'
    A = '/ai_pipeline/trade_data/workflow_a'
    # B
    try:
        L = json.loads(get(f'{B}/weekday/latest.json'))
        pkg = f"{B}/{L['package_path']}"
        man = json.loads(get(f'{pkg}/nas_manifest.json'))
        msha = sha(get(f'{pkg}/nas_manifest.json'))
        files = man.get('files', [])
        ok = bad = miss = 0
        for f in files:
            try:
                b = get(f"{pkg}/{f['path']}")
                if sha(b) == f['sha256'] and len(b) == f['bytes']: ok += 1
                else: bad += 1
            except Exception: miss += 1
        verdict = 'VERIFIED' if bad==0 and miss==0 else 'DEFECT'
        full.append(f"B包 {L['run_id']} rev {L['revision_id']}: manifest sha256={msha[:16]}… "
                   f"文件 {ok}/{len(files)} 匹配, {bad} 不符, {miss} 缺失 → {verdict}")
        full.append(f"  B execution_authorized = {L.get('execution_authorized')} (verbatim)")
        full.append(f"  B warnings count = {len(L.get('warnings', []))}")
        compact.append(f"B {L['run_id'].split('_')[0]} rev {L['revision_id'][:8]}: "
                       f"{ok}/{len(files)} {verdict} | exec_auth={L.get('execution_authorized')} | warnings={len(L.get('warnings', []))}")
    except Exception as e:
        full.append(f'B包核验失败: {e}')
        compact.append(f'B 核验失败: {e}')
    # A
    try:
        aw = json.loads(get(f'{A}/weekday/latest.json'))
        mp = aw.get('manifest_path')
        msha = sha(get(f'{A}/{mp}'))
        match = '匹配' if msha==aw.get('manifest_sha256') else '不匹配'
        full.append(f"A包 {aw.get('run_id')}: manifest_path={mp} sha256={msha[:16]}… "
                   f"(latest.json 载 manifest_sha256={aw.get('manifest_sha256','')[:16]}… {match})")
        full.append(f"  A optimizer_ready = {aw.get('optimizer_ready')} (verbatim)")
        full.append(f"  A publication_state = {aw.get('publication_state')}; source_status = {aw.get('source_status')}")
        compact.append(f"A {aw.get('run_id')}: sha {match} | optimizer_ready={aw.get('optimizer_ready')} | pub={aw.get('publication_state')}")
    except Exception as e:
        full.append(f'A包核验失败: {e}')
        compact.append(f'A 核验失败: {e}')
    # codex longbridge latest
    try:
        lb = json.loads(get('/ai_pipeline/trade_data/longbridge/latest.json'))
        base = '/ai_pipeline/trade_data/longbridge/'
        man = json.loads(get(f"{base}{lb['manifest_path']}"))
        msha = sha(get(f"{base}{lb['manifest_path']}"))
        files = man.get('files', [])
        ok = bad = miss = 0
        for f in files:
            try:
                b = get(f"{base}{lb['package_path']}/{f['path']}")
                if sha(b) == f['sha256'] and len(b) == f['bytes']: ok += 1
                else: bad += 1
            except Exception: miss += 1
        verdict = 'VERIFIED' if bad==0 and miss==0 else 'DEFECT'
        full.append(f"Longbridge包 {lb.get('run_id') or lb.get('review_id')} rev {lb.get('revision_id')}: "
                   f"manifest sha256={msha[:16]}… 文件 {ok}/{len(files)} 匹配, {bad} 不符, {miss} 缺失 → {verdict}")
        compact.append(f"LB {str(lb.get('run_id') or lb.get('review_id')).rsplit('_',1)[0]} rev {str(lb.get('revision_id'))[:8]}: "
                       f"{ok}/{len(files)} {verdict}")
    except Exception as e:
        full.append(f'Longbridge包核验失败: {e}')
        compact.append(f'LB 核验失败: {e}')
    return '\n'.join(full), '\n'.join(compact)

# ---- deterministic A/B context (bo: discussion must be based on A/B outputs) ----
def _book(path):
    d = json.loads(get(path))
    q = {}
    for p in d.get('positions') or []:
        s = (p.get('symbol') or (p.get('contract') or {}).get('symbol') or '')
        s = str(s).upper().split('.')[0]
        if s:
            q[s] = q.get(s, 0) + float(p.get('quantity') or 0)
    return q, d.get('created_at')

def ab_context():
    """Deterministic digest of the VERIFIED workflow A/B outputs + LATEST holdings.
    Every line is read from a packet field; nothing is invented."""
    lines = []
    B = '/ai_pipeline/trade_data/workflow_b'
    A = '/ai_pipeline/trade_data/workflow_a'
    # LATEST holdings (standard = latest snapshot, per bo)
    try:
        Lb = json.loads(get(f'{B}/weekday/latest.json'))
        t, tca = _book(f"{B}/{Lb['package_path']}/final_actual_portfolio_snapshot.json")
        lb = json.loads(get('/ai_pipeline/trade_data/longbridge/latest.json'))
        lq, lca = _book(f"/ai_pipeline/trade_data/longbridge/{lb['package_path']}/final_actual_portfolio_snapshot.json")
        ts = {k: v for k, v in t.items() if k in SEMI and v != 0}
        ls = {k: v for k, v in lq.items() if k in SEMI and v != 0}
        cross = {k: t.get(k, 0) + lq.get(k, 0) for k in set(t) | set(lq)
                 if k in SEMI and t.get(k, 0) + lq.get(k, 0) != 0}
        lines.append('## 最新持仓(以最新快照为标准, 已核验)')
        lines.append(f"Tiger(B, {str(tca)[:19]}): " + (', '.join(f'{k}={int(v)}' for k, v in sorted(ts.items())) or '无'))
        lines.append(f"Longbridge({str(lca)[:19]}): " + (', '.join(f'{k}={int(v)}' for k, v in sorted(ls.items())) or '无'))
        lines.append('跨账户半导体合计: ' + (', '.join(f'{k}={int(v)}' for k, v in sorted(cross.items())) or '无'))
    except Exception as e:
        lines.append(f'持仓读取失败: {e}')
    # B scores (review_action per held ticker)
    try:
        Lb = json.loads(get(f'{B}/weekday/latest.json'))
        pkg = f"{B}/{Lb['package_path']}"
        epa = [json.loads(x) for x in get(f'{pkg}/existing_position_actions.jsonl').decode().splitlines() if x.strip()]
        lines.append('')
        lines.append('## B 评分/审查 (existing_position_actions, 逐持仓)')
        for r in epa:
            tk = r.get('ticker')
            if not tk:
                continue
            ah = r.get('actual_holding') or {}
            qty = ah.get('quantity')
            rec = (r.get('recommendation') or '').split(';')[0].strip()
            ra = r.get('risk_actions') or {}
            lines.append(f"- {tk} qty={qty} review={rec} sell={ra.get('sell_action')}")
    except Exception as e:
        lines.append(f'B 评分读取失败: {e}')
    # A news (macro + verification + X opinions)
    try:
        aw = json.loads(get(f'{A}/weekday/latest.json'))
        pkg = f"{A}/{aw['manifest_path'].rsplit('/', 1)[0]}"
        lines.append('')
        lines.append('## A 新闻/信息 (workflow_a)')
        try:
            nv = json.loads(get(f'{pkg}/news_verification.json'))
            lines.append(f"核验: {nv.get('summary_zh','')}")
            if nv.get('excluded_numeric_claims'):
                lines.append('  已排除的异常数值: ' + ', '.join(nv['excluded_numeric_claims']))
        except Exception:
            pass
        seen = set()
        for l in get(f'{pkg}/telegram_news.jsonl').decode().splitlines():
            try:
                d = json.loads(l)
            except Exception:
                continue
            txt = (d.get('text') or '').replace('\n', ' ')
            key = txt[:40]
            if key in seen:
                continue
            seen.add(key)
            lines.append('- 新闻: ' + txt[:110])
        for l in get(f'{pkg}/x_items.jsonl').decode().splitlines():
            try:
                d = json.loads(l)
            except Exception:
                continue
            lines.append(f"- X({d.get('author','?')}): {d.get('summary_zh','')} [未独立核验]")
    except Exception as e:
        lines.append(f'A 新闻读取失败: {e}')
    return '\n'.join(lines)

def main():
    args = sys.argv[1:]
    dry = '--dry-run' in args
    window = '03:30'
    if '--window' in args:
        window = args[args.index('--window') + 1]
    duration = 50
    if '--duration' in args:
        duration = int(args[args.index('--duration') + 1])
    log(f'sa_0330 start (dry={dry}, window={window}, duration={duration}min)')
    global LLM_URL, LLM_MODEL
    LLM_URL, LLM_MODEL = detect_llm()
    if not dry and not LLM_URL:
        log('FATAL: no LLM endpoint healthy, aborting (no silent no-op)')
        sys.exit(3)
    log(f'LLM endpoint: {LLM_URL} model={LLM_MODEL}')
    # wait for the window to open (timer fires ~5 min early)
    h, m = (int(x) for x in window.split(':'))
    now = datetime.datetime.now()
    ws = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if ws > now:
        log(f'waiting {(ws - now).total_seconds()/60:.1f} min for window start {window}')
        time.sleep((ws - now).total_seconds())
    deadline = ws.timestamp() + duration * 60
    # regenerate base facts (gate flags / defects) then append A/B context
    try:
        import subprocess
        r = subprocess.run([sys.executable, f'{SA}/gen_facts.py'],
                           capture_output=True, text=True, timeout=120)
        log(f'SYSTEM.md regenerated (gen_facts rc={r.returncode})')
        if r.returncode != 0:
            log('gen_facts stderr: ' + r.stderr[-500:])
    except Exception as e:
        log(f'gen_facts failed: {e}')
    system = open(f'{SA}/SYSTEM.md', encoding='utf-8').read()
    ab = ab_context()
    system += '\n\n' + ab
    log('A/B context:\n' + ab)
    system += '''

# ═══ Q&A MODE (bo 授权, 2026-09-22) ═══
bo 委托你本窗口对 codex 的策略做"追问式校准", codex 会停留到窗口结束答疑。
核心问题(bo 指定): 是否应该提高半导体敞口 (should higher the semi exposure)。
讨论范围(bo 指定, 必须以此为基础):
  1) workflow A/B 的成果物 —— B 的评分/审查(review_action), A 的新闻/信息(见上方 A/B 上下文)。
  2) 最新持仓快照(见上方"最新持仓")。
  3) bo 在频道里的评论(以 codex 的口径为准)。
硬性规则:
- 绝不发决策指令、绝不下单、绝不批准/授权任何交易。你只追问和核验。
- 不重复核验持仓真实性, 不纠缠 SOXX 集中度这类无谓问题(bo 已明确: NAS 上最新的就是标准)。
- 每个问题必须落在 A/B 包内字段、最新持仓或 codex 本窗口原话上, 不引用包外数字。
- 回答要短、具体、中文。核验过的数字要肯定(VERIFIED), 没核验的说"未核验"。
- 若消息明显是发给另一个 agent 且没问你, 只输出: NO_REPLY
- 不要 @everyone/@here, 不要贴 token/webhook。
- 窗口结束前允许(且要求)发一段给 bo 的结论报告: 围绕核心问题"是否应提高半导体敞口"给出明确结论+依据(引用 B 评分/A 新闻/codex 原话)+核验数字, 是汇总不是指令。
'''
    # scan for new codex messages
    rows = read_inbox()
    seen = load_seen()
    codex_msgs = [m for m in rows if m.get('author_id') in CODEX_IDS
                  and int(m['id']) > 2000000000000000020 and m['id'] not in seen]
    # wait up to 5 min for the window's codex post if none yet
    wait_end = time.time() + 300
    while not codex_msgs and time.time() < wait_end:
        time.sleep(20)
        rows = read_inbox()
        codex_msgs = [m for m in rows if m.get('author_id') in CODEX_IDS
                      and int(m['id']) > 2000000000000000020 and m['id'] not in seen]
    log(f'codex msgs found: {len(codex_msgs)}')
    verif_full, verif = verify_packets()
    log('verification:\n' + verif_full)
    # initial message: deterministic verification + LLM probes (if codex already posted)
    calls = 0
    my_last = ''
    qa_log = []
    conclusion_posted = False
    codex_this_run = False
    if codex_msgs:
        codex_this_run = True
        probe_prompt = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content':
                '以下是我刚核验的事实(可直接引用):\n' + verif + '\n\n'
                'codex 在本窗口发布了策略(见下)。请围绕核心问题(是否应提高半导体敞口), '
                '基于 A/B 成果物(B 评分/A 新闻)与最新持仓, 写 2-3 个尖锐的追问问题'
                '(中文, 每个落在 A/B 包内字段或最新持仓上)。只输出问题本身, 不要前言, 不要核验块(我会自己加), '
                '不要下任何指令或订单, 不要重复核验持仓真实性。\n\n'
                + '\n\n'.join(f"[{m['author']}] {m['content']}" for m in codex_msgs[-3:])}
        ]
        probes = llm(probe_prompt, max_tokens=700, dry=dry)
        initial = ('[SA] [PIPE-20260922-SA-0330-01]\n'
                   f'{window} 策略包核验(我侧读回):\n' + verif + '\n\n'
                   '追问(围绕"是否提高半导体敞口", 基于 A/B 成果物, 仅校准非指令):\n' + probes)
        post(initial, dry=dry)
        calls = 1
        my_last = initial
    else:
        log(f'no codex post yet at window start; posting verification only, then waiting')
        initial = ('[SA] [PIPE-20260922-SA-0330-01]\n'
                   f'{window} 策略包核验(我侧读回, 等待 codex 发布后集中追问):\n' + verif)
        post(initial, dry=dry)
    seen.update(m['id'] for m in codex_msgs)
    save_seen(seen)
    if dry:
        log('dry-run: skipping Q&A loop'); return

    def post_conclusion():
        nonlocal calls
        cw = []
        for m in rows:
            if m.get('author_id') in CODEX_IDS and ((int(m['id']) >> 22) + 1420070400000) / 1000.0 > ws.timestamp():
                cw.append(m)
        cw.sort(key=lambda m: int(m['id']))
        qa_txt = '\n\n'.join(qa_log[-8:]) if qa_log else '(本运行无新 Q&A)'
        s_ctx = [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content':
                '本窗口 Q&A 阶段结束。给 bo 写一段中文结论报告, 直接可发 Discord(第一行 [SA] 结论报告)。\n'
                '核心问题(bo 指定): 是否应该提高半导体敞口 (should higher the semi exposure)。\n'
                '必须基于: B 评分/审查 + A 新闻 + 最新持仓 + codex 本窗口原话(见上下文)。\n'
                '结构:\n'
                '1) 结论: 提高 / 不提高 / 有条件提高 —— 一句话直接回答\n'
                '2) 依据: codex 本窗口立场与理由(引用其原话要点)\n'
                '3) A/B 支撑: 用 B 的评分/审查 + A 的新闻来支撑或反驳该结论(点名具体 ticker 与新闻)\n'
                '4) 核验事实: 最新半导体持仓与跨账户合计(用我核验的数字), 各包 VERIFIED 状态\n'
                '5) 触发条件: 什么情况下应改变这个决定\n'
                '6) 未核验/待确认项\n'
                '只输出报告本身, 不要前言, 不要 @everyone, 不要下指令, 不要重复核验持仓真实性, 16 行内。\n\n'
                '核验事实:\n' + verif + '\n\n'
                '本窗口 codex 发布(摘录):\n' + ('\n\n'.join(f"[{m['author']}] {m['content'][:400]}" for m in cw[-6:]) or '(无)') + '\n\n'
                '本运行 Q&A 摘录:\n' + qa_txt},
        ]
        summary = llm(s_ctx, max_tokens=700)
        post(summary)
        calls += 1
        log('conclusion report posted')

    # Q&A loop
    while time.time() < deadline and calls < MAX_CALLS:
        time.sleep(POLL)
        rows = read_inbox()
        new = [m for m in rows if m.get('author_id') in CODEX_IDS
               and m['id'] not in seen]
        for m in new:
            seen.add(m['id'])
            save_seen(seen)
            codex_this_run = True
            log(f'new codex msg {m["id"]} from {m["author"]}')
            ctx = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content':
                    '我(SA)刚发的核验+追问:\n' + my_last[:1500] + '\n\n'
                    'codex 回复:\n' + m['content'] + '\n\n'
                    '请按 Q&A MODE 规则, 用中文简短回应: 围绕核心问题(是否提高半导体敞口), '
                    '基于 A/B 成果物与最新持仓核验其数字(VERIFIED/未核验/CONTRADICTED)并继续追问。'
                    '不下指令, 不批准, 不引用包外数字, 不重复核验持仓真实性, 不纠缠 SOXX 集中度。'
                    '若其消息明显发给另一个 agent 且没问你, 只输出 NO_REPLY。'},
            ]
            reply = llm(ctx, max_tokens=700)
            calls += 1
            if reply.strip().upper() == 'NO_REPLY':
                log('NO_REPLY, skipping'); continue
            post(reply)
            my_last = reply
            qa_log.append(f'[codex-{m["author"]}] {m["content"][:300]}\n[SA] {reply[:300]}')
        # codex quiet 10min (after posting this run) → post conclusion report to bo (once)
        if not conclusion_posted and codex_this_run:
            lts = 0
            for m in rows:
                if m.get('author_id') in CODEX_IDS:
                    lts = max(lts, (int(m['id']) >> 22) + 1420070400000)
            lts /= 1000.0
            if lts > ws.timestamp() and time.time() - lts > 600:
                log('codex quiet 10min; posting conclusion report to bo')
                post_conclusion()
                conclusion_posted = True
    # catch-up: any codex post that arrived after the loop exited gets one reply
    time.sleep(POLL)
    rows = read_inbox()
    late = [m for m in rows if m.get('author_id') in CODEX_IDS and m['id'] not in seen]
    if late:
        log(f'catch-up: {len(late)} late codex msg(s)')
        for m in late[:3]:
            seen.add(m['id']); save_seen(seen)
            ctx = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content':
                    '我(SA)此前发的核验+追问:\n' + my_last[:1500] + '\n\n'
                    'codex 稍后回复:\n' + m['content'] + '\n\n'
                    '请按 Q&A MODE 规则, 用中文简短回应: 围绕核心问题(是否提高半导体敞口), '
                    '基于 A/B 成果物与最新持仓核验其数字并继续追问。不下指令, 不批准, 不引用包外数字, '
                    '不重复核验持仓真实性。若其消息明显发给另一个 agent 且没问你, 只输出 NO_REPLY。'},
            ]
            reply = llm(ctx, max_tokens=700)
            if reply.strip().upper() == 'NO_REPLY':
                log('catch-up NO_REPLY, skipping'); continue
            post(reply)
            my_last = reply
            qa_log.append(f'[codex-{m["author"]}] {m["content"][:300]}\n[SA] {reply[:300]}')
            calls += 1
    if not conclusion_posted:
        log('window closed; posting final conclusion report to bo')
        post_conclusion()
        conclusion_posted = True
    log(f'sa_0330 done, {calls} LLM calls')

if __name__ == '__main__':
    main()
