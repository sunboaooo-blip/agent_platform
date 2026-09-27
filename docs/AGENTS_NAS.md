# AGENTS_NAS.md — NAS Auto-Upload Pipeline (Progress Doc)

> Last updated: 2026-09-06 22:30 JST
> Status: ✅ **Built and verified end-to-end**

## 1. Goal
Automatically upload files from this host (`$GPU_NODE_HOST`) to the local UGREEN NAS.
- NAS: UGOS Pro, `http://nas.example.internal:9999/desktop`
- Usage: **drop files into `~/nas-drop/` → auto-uploaded to NAS `/home/sba/ai_pipeline/`** (path changed 2026-09-06)

## 2. Probe Findings (2026-09-06)
| Channel | Port | Status | Notes |
|---|---|---|---|
| **WebDAV** | **5005** | ✅ in use | Basic auth, standard protocol, pure Python stdlib |
| SMB | 445 | open but unusable | no smbclient/mount.cifs on host, no sudo |
| UGOS Web API | 9999 | open | login needs RSA+AES-GCM encryption; used only for storage queries |
| SSH | 22 | closed | — |

- Host constraints: no pip/ensurepip/sudo; python3 + requests available → everything implemented with stdlib (+ `cryptography` for the API client)
- WebDAV root `/` does NOT allow MKCOL of new top-level dirs (405); must write inside existing shares
- Writable NAS locations: `/home/`, `/project-nomad/`, `/ai_pipeline/`, `/docker/`
- Upload destination: `/home/sba/ai_pipeline/` (created via MKCOL)

## 3. Credentials
- Account: `YOUR_NAS_USER` / Password: `YOUR_NAS_PASSWORD`  
  (see `nas-config.example.json`; real values live only in `~/nas-upload/config.json`)
- Stored in: `~/nas-upload/config.json` (chmod 600)
- ⚠️ Password is plaintext in config.json — do not share that file

## 4. Components
```
~/nas-upload/
├── nas_upload.py     # manual upload CLI (WebDAV client, stdlib)
├── nas_watch.py      # auto-upload watcher (polls drop dir)
├── nas_storage.py    # storage status query (UGOS encrypted API: RSA+AES-GCM login)
├── config.json       # config (host/port/user/password/paths, 600)
├── upload.log        # manual upload log
├── watch.log         # auto upload log
└── watch_state.json  # watcher state (file-stability tracking)

~/nas-drop/    # ★ drop files here (auto-upload entry)
├── done/             # successfully uploaded files moved here
└── failed/           # failed uploads moved here

~/.config/systemd/user/nas-upload.service   # systemd user service
```

## 5. How It Works
1. `nas_watch.py` polls `~/nas-drop/` every 5s (ignores done/, failed/, dotfiles)
2. A new file/dir must have **stable total size for 2 consecutive polls** before upload (avoids uploading half-written files)
3. Directories are uploaded recursively, preserving relative structure; target = `/home/sba/ai_pipeline/<original name>`
4. Success → moved to `done/`; failure → moved to `failed/`
5. systemd user service keeps it running; auto-restart 10s after crash; starts on login

## 6. Common Commands
```bash
# service status / logs
systemctl --user status nas-upload.service
journalctl --user -u nas-upload.service -f
tail -f ~/nas-upload/watch.log

# manual upload (bypasses drop dir)
python3 ~/nas-upload/nas_upload.py <file/dir> --dest <NAS subdir>
python3 ~/nas-upload/nas_upload.py --check              # test connection
python3 ~/nas-upload/nas_upload.py --list /home/sba/ai_pipeline   # list NAS contents

# change NAS destination / poll interval: edit ~/nas-upload/config.json then
systemctl --user restart nas-upload.service

# query NAS storage status (capacity / disks / temps)
python3 ~/nas-upload/nas_storage.py
```

## 7. UGOS Encrypted API (nas_storage.py)
Reverse-engineered login flow (all implemented):
1. POST `/ugreen/v1/verify/check` `{username}` → response header `X-Rsa-Token` (RSA public key, base64)
2. RSA-PKCS1v15 encrypt password → POST `/ugreen/v1/verify/login` → `data.token` + `data.public_key`
3. Every API request: AES-256-GCM encrypt body (iv+ct+tag, base64); headers:
   - `X-Ugreen-Token` = base64(RSA(login token, public_key from login))
   - `X-Ugreen-Security-Key` = MD5(login token)
   - `X-Ugreen-Security-Code` = base64(RSA(aes_key_hex, public_key))
4. Response `encrypt_resp_body` decrypted with the same AES key

Storage APIs: GET `/ugreen/v1/storage/pool/list`, `/ugreen/v2/storage/disk/list`
Depends on: `cryptography` package (installed on host)

### Latest storage snapshot (2026-09-06)
- NAS: DXP2800-XXXX (model + serial suffix redacted), UGOS Pro 1.15.1.0127
- Pool 1 (basic, M.2 NX-512 476.9GB): 8.3% used, 415.6GB free — mounted `/volume1`, `/home` (upload destination lives here)
- Pool 2 (basic, Seagate IronWolf 4TB): 9.4% used, 3.2TB free — mounted `/volume2`
- Both disks healthy: 48°C / 41°C, ~251h / 13h power-on

## 8. Verified
- [x] WebDAV auth (PROPFIND 207)
- [x] PUT upload (201 Created)
- [x] MKCOL directory creation (201)
- [x] End-to-end: `e2e-test.txt` dropped → auto-uploaded → confirmed on NAS
- [x] systemd service running (enabled, active)
- [x] Fixed: first-round `NoneType` exception for new jobs (2026-09-06)
- [x] 2026-09-06: destination changed to `/home/sba/ai_pipeline/`, verified with `path-test.txt`
- [x] 2026-09-06: UGOS encrypted login + storage query working (`nas_storage.py`)

## 8b. Service auto-start (verified 2026-09-13)

`nas-upload.service` is **enabled + lingering**, so it starts ~10 s after every
boot with no login required (`Linger=yes`, `/var/lib/systemd/linger/$USER`).
Restart policy changed 2026-09-13 from `on-failure` to **`always`, `RestartSec=15`**
— the watcher is meant to be permanently on, so a clean exit or an accidental
manual stop now comes back on its own. Verified by `kill -9`: restarted in 15 s
and immediately processed a new drop. `RestartSec` (15 s) deliberately exceeds
`StartLimitIntervalSec` (10 s) so a crash loop cannot wedge the unit.

Gotcha: a `systemctl --user stop` from a shell used to stay stopped indefinitely
(this is what left it down for 19 h on 2026-09-12 during the flashnext tuning
session). With `Restart=always` systemd still honours an explicit stop for that
invocation, so if it looks dead, check `is-active` before assuming boot failed.

Gotcha: the watcher uploads `~/nas-drop/<name>` to the **root** of
`/home/sba/ai_pipeline/`, not into `local_agent/`. To target a subdirectory use
the manual CLI: `nas_upload.py <file> --dest home/sba/ai_pipeline/local_agent`.

## 9. Known Limitations / TODO
- Large files are read fully into memory before PUT; for multi-GB single files, consider streaming/chunking (not implemented)
- No resume/chunked upload; a failed large file re-uploads from scratch
- Same-name files are overwritten (WebDAV PUT semantics)
- If the NAS WebDAV port changes after a reboot, update config.json
- Optional: retry on failure (currently just moves to failed/)
