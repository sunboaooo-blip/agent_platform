#!/usr/bin/env python3
"""
nas_upload.py — 纯 Python 标准库 WebDAV 上传 pipeline
把本机文件/目录上传到绿联 UGOS Pro NAS (nas.example.internal:5005)

用法:
  nas_upload.py <本地路径>... [--dest NAS上的目标目录] [--force] [--list] [--check]
  nas_upload.py --setup          # 交互式填写账号密码
  nas_upload.py --check          # 测试 NAS 连接
  nas_upload.py --list [远程目录] # 列出 NAS 上的内容
"""
import base64, json, os, sys, time, argparse, urllib.request, urllib.error, urllib.parse, hashlib, mimetypes
from concurrent.futures import ThreadPoolExecutor, as_completed

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload.log")

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(CONFIG_PATH, 0o600)

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def auth_header(cfg):
    token = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}

class WebDAV:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = f"http://{cfg['host']}:{cfg['port']}"
        self.headers = auth_header(cfg)

    def _request(self, method, path, data=None, extra=None):
        url = self.base + urllib.parse.quote(path, safe="/")
        headers = dict(self.headers)
        if extra: headers.update(extra)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.get("timeout", 60)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def check(self):
        status, _ = self._request("PROPFIND", "/", extra={"Depth": "0"})
        return status

    def listdir(self, path="/"):
        status, body = self._request("PROPFIND", path, extra={"Depth": "1"})
        if status != 207:
            return None, status
        # 解析 multistatus XML
        entries = []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(body)
        ns = {"d": "DAV:"}
        for resp in root.findall("d:response", ns):
            href = resp.find("d:href", ns).text
            prop = resp.find("d:propstat/d:prop", ns)
            if prop is None: continue
            name = os.path.basename(href.rstrip("/"))
            ftype = prop.findtext("d:resourcetype/d:collection", default=None, namespaces=ns)
            size = prop.findtext("d:getcontentlength", default="0", namespaces=ns)
            mod = prop.findtext("d:getlastmodified", default="", namespaces=ns)
            if href.rstrip("/") != path.rstrip("/"):
                entries.append({
                    "name": name,
                    "is_dir": ftype is not None,
                    "size": int(size or 0),
                    "modified": mod
                })
        return entries, status

    def exists(self, path):
        status, _ = self._request("PROPFIND", path, extra={"Depth": "0"})
        return status in (200, 207)

    def mkdir(self, path):
        status, _ = self._request("MKCOL", path)
        return status in (201, 204)

    def put_file(self, local_path, remote_path):
        status, _ = self._request("PUT", remote_path, data=open(local_path, "rb").read())
        return status in (200, 201, 204)

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def human(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024: return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"

def remote_join(root, *parts):
    p = root.rstrip("/") + "/" + "/".join(parts)
    return p

def ensure_remote_dirs(cfg, remote_dir):
    """逐级创建远程目录"""
    d = WebDAV(cfg)
    if d.exists(remote_dir): return True
    # 逐级 MKCOL
    parts = [x for x in remote_dir.split("/") if x]
    cur = ""
    for part in parts:
        cur = cur + "/" + part
        if not d.exists(cur):
            d.mkdir(cur)
    return d.exists(remote_dir)

def upload_one(cfg, local, remote):
    """上传单个文件, 返回 (local, remote, ok, msg)"""
    d = WebDAV(cfg)
    rdir = os.path.dirname(remote)
    if rdir and not d.exists(rdir):
        ensure_remote_dirs(cfg, rdir)
    ok = d.put_file(local, remote)
    return local, remote, ok, "ok" if ok else "FAILED"

def collect_files(local_paths, dest):
    """收集要上传的文件列表: [(local_path, remote_path)]"""
    cfg = load_config()
    files = []
    for lp in local_paths:
        lp = os.path.abspath(lp)
        if os.path.isdir(lp):
            base = os.path.basename(lp.rstrip("/"))
            for root, dirs, names in os.walk(lp):
                for n in names:
                    full = os.path.join(root, n)
                    rel = os.path.relpath(full, lp)
                    remote = remote_join(cfg["remote_root"], dest, base, rel)
                    files.append((full, remote))
        elif os.path.isfile(lp):
            remote = remote_join(cfg["remote_root"], dest, os.path.basename(lp))
            files.append((lp, remote))
        else:
            log(f"跳过(不存在): {lp}")
    return files

def cmd_check(cfg):
    d = WebDAV(cfg)
    st = d.check()
    if st == 207:
        log("✓ 连接成功, 已认证")
        entries, _ = d.listdir("/")
        if entries:
            print("  根目录内容:")
            for e in entries[:30]:
                t = "📁" if e["is_dir"] else "📄"
                print(f"  {t} {e['name']}  {human(e['size']) if not e['is_dir'] else ''}")
        return 0
    elif st == 401:
        log("✗ 认证失败 (401) — 请运行 nas_upload.py --setup 填写正确的账号密码")
        return 1
    else:
        log(f"✗ 连接异常, HTTP {st}")
        return 1

def cmd_setup():
    cfg = load_config()
    print("=== NAS 配置 ===")
    print(f"当前: {cfg['host']}:{cfg['port']}  用户='{cfg['user']}'  远程根='{cfg['remote_root']}'")
    u = input(f"NAS 用户名 (回车跳过): ").strip()
    if u: cfg["user"] = u
    p = input("NAS 密码 (回车跳过): ").strip()
    if p: cfg["password"] = p
    r = input(f"远程根目录 (回车保持 '{cfg['remote_root']}'): ").strip()
    if r: cfg["remote_root"] = r if r.startswith("/") else "/" + r
    save_config(cfg)
    log("配置已保存")
    # 立即测试
    cmd_check(cfg)

def cmd_list(cfg, path):
    d = WebDAV(cfg)
    p = path or cfg["remote_root"]
    entries, st = d.listdir(p)
    if st != 207:
        log(f"✗ 列目录失败, HTTP {st}")
        return 1
    if not entries:
        print("(空目录)")
        return 0
    for e in entries:
        t = "📁" if e["is_dir"] else "📄"
        size = human(e["size"]) if not e["is_dir"] else ""
        print(f"{t} {e['name']:<40} {size:<12} {e['modified']}")
    return 0

def cmd_upload(cfg, local_paths, dest, force, workers=4):
    files = collect_files(local_paths, dest)
    if not files:
        log("没有可上传的文件")
        return 0
    total = sum(os.path.getsize(f) for f, _ in files)
    log(f"共 {len(files)} 个文件, 总大小 {human(total)}, 目标: {cfg['remote_root']}/{dest}")

    # 预创建目标目录
    ensure_remote_dirs(cfg, remote_join(cfg["remote_root"], dest))

    done, failed, skipped = 0, 0, 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(upload_one, cfg, l, r): (l, r) for l, r in files}
        for fut in as_completed(futs):
            l, r, ok, msg = fut.result()
            if ok:
                done += 1
                log(f"✓ {os.path.basename(l)} -> {r}")
            else:
                failed += 1
                log(f"✗ {l} -> {r} ({msg})")
    elapsed = time.time() - t0
    speed = total / elapsed / 1024 / 1024 if elapsed > 0 else 0
    log(f"完成: 成功 {done}, 失败 {failed}, 用时 {elapsed:.1f}s, 平均 {speed:.2f} MB/s")
    return 0 if failed == 0 else 1

def main():
    ap = argparse.ArgumentParser(description="NAS WebDAV 上传 pipeline")
    ap.add_argument("paths", nargs="*", help="要上传的本地文件/目录")
    ap.add_argument("--dest", default="Uploads", help="NAS 上的目标子目录 (默认 Uploads)")
    ap.add_argument("--force", action="store_true", help="强制覆盖")
    ap.add_argument("--setup", action="store_true", help="交互式配置账号")
    ap.add_argument("--check", action="store_true", help="测试连接")
    ap.add_argument("--list", action="store_true", help="列出 NAS 内容")
    ap.add_argument("--workers", type=int, default=4, help="并发数 (默认 4)")
    args = ap.parse_args()

    cfg = load_config()

    if args.setup:
        sys.exit(cmd_setup())
    if args.check:
        sys.exit(cmd_check(cfg))
    if args.list:
        sys.exit(cmd_list(cfg, args.paths[0] if args.paths else None))

    if not cfg.get("user") or not cfg.get("password"):
        log("尚未配置账号密码, 请先运行: nas_upload.py --setup")
        sys.exit(1)

    if not args.paths:
        ap.print_help()
        sys.exit(1)

    sys.exit(cmd_upload(cfg, args.paths, args.dest, args.force, args.workers))

if __name__ == "__main__":
    main()
