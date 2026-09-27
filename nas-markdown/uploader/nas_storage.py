#!/usr/bin/env python3
"""
nas_storage.py — 查询 UGOS Pro NAS 存储状态 (完整加密登录)
用法: python3 nas_storage.py
"""
import base64, json, os, uuid, hashlib, sys, urllib.request, urllib.error
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE = os.environ.get("NAS_API_BASE", "http://nas.example.internal:9999/ugreen")
CFG = json.load(open(os.path.expanduser(
    os.environ.get("NAS_CFG", "~/nas-upload/config.json"))))

def req(path, data=None, headers=None, method=None):
    url = BASE + path
    h = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def rsa_enc(pub, msg): return pub.encrypt(msg, padding.PKCS1v15())
def aesgcm_b64(key_hex, data):
    iv = uuid.uuid4().bytes[:12]
    return base64.b64encode(iv + AESGCM(key_hex.encode()).encrypt(iv, data.encode(), None)).decode()
def aesgcm_dec(key_hex, b64):
    raw = base64.b64decode(b64)
    return AESGCM(key_hex.encode()).decrypt(raw[:12], raw[12:], None).decode()

# ---- 登录 ----
st, hdrs, body = req("/v1/verify/check", {"username": CFG["user"]})
b64key = hdrs.get("X-Rsa-Token") or hdrs.get("x-rsa-token")
if not b64key:
    print("登录失败: 无公钥"); sys.exit(1)
pub_pw = load_pem_public_key(base64.b64decode(b64key))
enc_pw = base64.b64encode(rsa_enc(pub_pw, CFG["password"].encode())).decode()
st, hdrs, body = req("/v1/verify/login", {"username": CFG["user"], "password": enc_pw, "keepalive": True, "otp": False, "is_simple": False})
d = json.loads(body)["data"]
if "token" not in d:
    print("登录失败:", body[:200]); sys.exit(1)
token = d["token"]
pub_api = load_pem_public_key(base64.b64decode(d["public_key"]))

def api(path, query=""):
    aes_key = uuid.uuid4().hex
    headers = {
        "X-Ugreen-Token": base64.b64encode(rsa_enc(pub_api, token.encode())).decode(),
        "X-Ugreen-Security-Key": hashlib.md5(token.encode()).hexdigest(),
        "X-Ugreen-Security-Code": base64.b64encode(rsa_enc(pub_api, aes_key.encode())).decode(),
    }
    if query:
        data = {"encrypt_query": aesgcm_b64(aes_key, query)}
    else:
        data = {"encrypt_req_body": aesgcm_b64(aes_key, "{}"), "req_body_sha256": hashlib.sha256(b"{}").hexdigest()}
    st, _, body = req(path, data=data, headers=headers, method="GET")
    j = json.loads(body)
    if "encrypt_resp_body" in j:
        return json.loads(aesgcm_dec(aes_key, j["encrypt_resp_body"]))
    return j

def h(n):
    n = float(n)
    for u in ["B","KB","MB","GB","TB","PB"]:
        if n < 1024: return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}EB"

print("=" * 52)
print(f"NAS 存储状态  ({d.get('nas_name','?')}  {d.get('model','')}  {d.get('system_version','')})")
print("=" * 52)

pools = api("/v1/storage/pool/list").get("data", {}).get("result", [])
print(f"\n--- 存储池 ({len(pools)}) ---")
for p in pools:
    print(f"\n【{p.get('label', p.get('name'))}】  RAID: {p.get('level')}  状态: {p.get('status')}")
    for vol in p.get("volumes", []):
        t, u, a = vol.get("total",0), vol.get("used",0), vol.get("available",0)
        pct = u/t*100 if t else 0
        bar = "█"*int(pct/5) + "░"*(20-int(pct/5))
        print(f"   卷 {vol.get('name')}  ({vol.get('filesystem')}, 挂载 {','.join(vol.get('mntpaths',[]))})")
        print(f"   {bar} {pct:.1f}%")
        print(f"   总容量 {h(t)} | 已用 {h(u)} | 可用 {h(a)}")

disks = api("/v2/storage/disk/list").get("data", {}).get("result", [])
print(f"\n--- 物理磁盘 ({len(disks)}) ---")
for dk in disks:
    print(f"  {dk.get('label','?'):<16} {dk.get('brand','?')} {dk.get('model','')}")
    print(f"     容量 {h(dk.get('size',0))} | 接口 {dk.get('interface_type')} | 温度 {dk.get('temperature','?')}°C | 通电 {dk.get('power_on_hours',0)}h")
    print(f"     用途: {dk.get('used_for','未分配')} | 状态: {dk.get('status')}")

print()
