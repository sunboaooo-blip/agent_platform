#!/usr/bin/env python3
"""
nas_watch.py — 自动上传 watcher
轮询 ~/nas-drop/，把新出现的文件/目录自动上传到 NAS /home/sba/
- 文件稳定判定: 连续两次轮询总大小不变才上传 (防止传一半的文件)
- 成功后移入 nas-drop/done/，失败移入 nas-drop/failed/
- 日志: nas-upload/watch.log
"""
import json, os, sys, time, shutil, hashlib
from nas_upload import WebDAV, load_config, log, ensure_remote_dirs, human

DROP = None
DEST = None
POLL = 5
STATE_FILE = None
WATCH_LOG = None
STABLE_ROUNDS = 2

def wlog(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(WATCH_LOG, "a") as f:
        f.write(line + "\n")

def tree_size(path):
    total = 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for root, _, names in os.walk(path):
        for n in names:
            try: total += os.path.getsize(os.path.join(root, n))
            except OSError: pass
    return total

def upload_tree(cfg, local, remote):
    """递归上传文件或目录到 remote 路径"""
    d = WebDAV(cfg)
    if os.path.isfile(local):
        rdir = os.path.dirname(remote)
        if rdir and not d.exists(rdir):
            ensure_remote_dirs(cfg, rdir)
        return d.put_file(local, remote)
    # 目录
    ensure_remote_dirs(cfg, remote)
    ok = True
    for root, dirs, names in os.walk(local):
        rel = os.path.relpath(root, local)
        rdir = remote if rel == "." else remote + "/" + rel
        for n in dirs:
            if not d.exists(rdir + "/" + n):
                d.mkdir(rdir + "/" + n)
        for n in names:
            if not d.put_file(os.path.join(root, n), rdir + "/" + n):
                ok = False
                wlog(f"  ✗ 子文件失败: {os.path.join(root, n)}")
    return ok

def main():
    global DROP, DEST, POLL, STATE_FILE, WATCH_LOG
    cfg = load_config()
    DROP = cfg["drop_dir"]
    DEST = cfg["remote_drop_dest"]
    POLL = cfg.get("poll_seconds", 5)
    STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch_state.json")
    WATCH_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch.log")
    os.makedirs(os.path.join(DROP, "done"), exist_ok=True)
    os.makedirs(os.path.join(DROP, "failed"), exist_ok=True)

    wlog(f"watcher 启动: {DROP} -> {DEST} (每 {POLL}s 轮询)")
    state = {}  # name -> {"size": int, "stable": int}

    while True:
        try:
            entries = [e for e in os.listdir(DROP)
                       if e not in ("done", "failed") and not e.startswith(".")]
            # 清理已消失的 job
            for name in list(state.keys()):
                if name not in entries:
                    del state[name]

            for name in entries:
                path = os.path.join(DROP, name)
                size = tree_size(path)
                st = state.get(name)
                if st is None:
                    state[name] = {"size": size, "stable": 0}
                    st = state[name]
                    wlog(f"发现新任务: {name} ({human(size)})")
                elif st["size"] == size:
                    st["stable"] += 1
                else:
                    st["size"], st["stable"] = size, 0

                if st["stable"] >= STABLE_ROUNDS:
                    wlog(f"开始上传: {name} ({human(size)})")
                    remote = DEST.rstrip("/") + "/" + name
                    try:
                        ok = upload_tree(cfg, path, remote)
                    except Exception as e:
                        ok = False
                        wlog(f"  ✗ 异常: {e}")
                    if ok:
                        shutil.move(path, os.path.join(DROP, "done", name))
                        wlog(f"✓ 完成: {name} -> {remote}")
                    else:
                        shutil.move(path, os.path.join(DROP, "failed", name))
                        wlog(f"✗ 失败: {name} (已移入 failed/)")
                    del state[name]
        except KeyboardInterrupt:
            wlog("watcher 停止")
            break
        except Exception as e:
            wlog(f"轮询异常: {e}")
        time.sleep(POLL)

if __name__ == "__main__":
    main()
