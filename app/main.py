"""Homelab dashboard: FastAPI backend. Read-mostly; only start/stop/restart on containers."""
import base64, hashlib, hmac, json, logging, os, re, secrets, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import docker
import psutil
import yaml
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------- config
# Settings come from an optional YAML file (see config.example.yaml); a few can be overridden by
# environment variables. Everything has a safe default, so the file may be missing.
def load_config():
    path = os.environ.get("DASH_CONFIG", "/config/config.yaml")
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as e:
        sys.exit(f"FATAL: cannot read config file {path}: {type(e).__name__}")
    if not isinstance(cfg, dict):
        sys.exit(f"FATAL: config file {path} must be a YAML mapping")
    return cfg


CFG = load_config()


def cfg(*keys, default=None):
    cur = CFG
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return default if cur is None else cur


def check_password_hash(value: str):
    """Return an error string if the stored hash is missing or malformed, else None. Never echoes the value."""
    if not value:
        return "DASH_PASSWORD_HASH is not set"
    try:
        scheme, n, r, p, salt, want = value.split(":")
        n, r, p = int(n), int(r), int(p)
        base64.urlsafe_b64decode(salt), base64.urlsafe_b64decode(want)
    except Exception:
        return "DASH_PASSWORD_HASH is malformed"
    if scheme != "scrypt" or not (2 ** 12 <= n <= 2 ** 20 and n & (n - 1) == 0) or not (1 <= r <= 32) or not (1 <= p <= 16):
        return "DASH_PASSWORD_HASH has unsupported parameters"
    return None


DATA_DIR = os.environ.get("DATA_DIR", "/data")
PASSWORD_HASH = os.environ.get("DASH_PASSWORD_HASH", "").strip()
# No default password exists. Refuse to start until one has been set with ./set-password.sh.
_pw_problem = check_password_hash(PASSWORD_HASH)
if _pw_problem:
    sys.exit(f"FATAL: {_pw_problem}. Refusing to start without a password. "
             "Run ./set-password.sh on the host, then: docker compose up -d --force-recreate dashboard")

TITLE = str(os.environ.get("DASH_TITLE") or cfg("title", default="Homelab Dashboard"))[:60]
_raid_dev = os.environ.get("RAID_DEVICE")
RAID_DEVICE = str(_raid_dev if _raid_dev is not None else cfg("raid", "device", default="md0") or "").strip()
if RAID_DEVICE and not re.fullmatch(r"md\d+", RAID_DEVICE):
    sys.exit("FATAL: raid.device / RAID_DEVICE must look like md0 (or be empty to hide the RAID section)")
SELF_PROJECT = str(cfg("self", "project", default="dashboard"))
SELF_NAMES = set(cfg("self", "container_names", default=["dashboard", "dashboard-socket-proxy"]))
try:
    CONFIRM_RE = re.compile(str(cfg("confirm_before_stop", default=r"pi-?hole|adguard|cloudflared|tailscale|wireguard")), re.I)
except re.error:
    sys.exit("FATAL: confirm_before_stop in the config is not a valid regular expression")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SESSION_TTL = 12 * 3600
IDLE_AFTER = 60            # stop sampling when nobody has polled for this long
RAM_WARN, SWAP_WARN, DISK_CRIT = 90, 50, 90
SMART_STALE_AFTER = int(cfg("smart", "stale_after_minutes", default=30)) * 60
SMART_TEMP_WARN = int(cfg("smart", "temp_warn_c", default=55))
# Extra filesystems to show. Each path is where the compose file mounts it inside the container.
MOUNTS = [{"label": str(m.get("label", m.get("path"))), "path": str(m["path"])}
          for m in cfg("mounts", default=[]) if isinstance(m, dict) and m.get("path")]
HOST_PATHS = {"md": "/host/md", "mdstat": "/host/mdstat", "net": "/host/net",
              "smart": "/host/smart/smart.json"}

# ---------------------------------------------------------------- audit log
os.makedirs(DATA_DIR, exist_ok=True)
_audit = logging.getLogger("audit")
_audit.setLevel(logging.INFO)
_fh = RotatingFileHandler(os.path.join(DATA_DIR, "actions.log"), maxBytes=1_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(message)s"))
_audit.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("audit %(message)s"))
_audit.addHandler(_sh)


def audit(event, ip, **kw):
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, "ip": ip, **kw}
    _audit.info(json.dumps(rec))


def audit_tail(n=30):
    try:
        with open(os.path.join(DATA_DIR, "actions.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out[::-1]

# ---------------------------------------------------------------- auth
SESSIONS = {}              # sid -> {"csrf", "exp"}
_slock = threading.Lock()
FAILS = {}                 # ip -> [timestamps]
GLOBAL_FAILS = []
LOCK_WINDOW, LOCK_MAX, GLOBAL_MAX = 600, 5, 30


def verify_password(pw: str) -> bool:
    try:
        scheme, n, r, p, salt, want = PASSWORD_HASH.split(":")
        got = hashlib.scrypt(pw.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r),
                             p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
        return scheme == "scrypt" and hmac.compare_digest(got, base64.urlsafe_b64decode(want))
    except Exception:
        return False


def _prune(lst, now):
    lst[:] = [t for t in lst if now - t < LOCK_WINDOW]


def login_blocked(ip):
    now = time.time()
    lst = FAILS.setdefault(ip, [])
    _prune(lst, now)
    _prune(GLOBAL_FAILS, now)
    if len(lst) >= LOCK_MAX:
        return int(LOCK_WINDOW - (now - lst[0])) + 1
    if len(GLOBAL_FAILS) >= GLOBAL_MAX:
        return int(LOCK_WINDOW - (now - GLOBAL_FAILS[0])) + 1
    return 0


def get_session(request: Request):
    sid = request.cookies.get("__Host-session")
    with _slock:
        now = time.time()
        for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
            del SESSIONS[k]
        s = SESSIONS.get(sid) if sid else None
    return (sid, s)


def client_ip(request: Request):
    return request.client.host if request.client else "?"


def deny(code, msg, **extra):
    return JSONResponse({"error": msg, **extra}, status_code=code)


def check_post(request: Request, session=None):
    """Origin must match Host; if a session is given its CSRF token must match too."""
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host or origin.split("://", 1)[-1] != host:
        return deny(403, "bad origin")
    if session is not None:
        tok = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(tok, session["csrf"]):
            return deny(403, "bad csrf token")
    return None

# ---------------------------------------------------------------- host sampling
STATE = {"host": None, "raid": None, "smart": None, "containers": None, "ts": 0}
last_seen = 0.0
_prev_net = {}


def read(path):
    with open(path) as f:
        return f.read().strip()


def sample_net():
    """Host NIC counters. /proc/net/dev inside a container is the container's own, so the host's
    sysfs statistics dir is bind-mounted per interface at /host/net/<iface> instead."""
    out, now = [], time.time()
    base = HOST_PATHS["net"]
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        try:
            rx, tx = int(read(f"{base}/{name}/rx_bytes")), int(read(f"{base}/{name}/tx_bytes"))
        except (OSError, ValueError):
            continue
        prev = _prev_net.get(name)
        _prev_net[name] = (now, rx, tx)
        rate = [0, 0] if not prev or now <= prev[0] else [max(0, (rx - prev[1]) / (now - prev[0])),
                                                           max(0, (tx - prev[2]) / (now - prev[0]))]
        out.append({"name": name, "rx_bps": rate[0], "tx_bps": rate[1], "rx_total": rx, "tx_total": tx})
    return out


def disk_usage(label, path):
    try:
        u = psutil.disk_usage(path)
        return {"label": label, "total": u.total, "used": u.used, "percent": round(u.used / (u.used + u.free) * 100, 1)}
    except OSError as e:
        return {"label": label, "error": str(e)}


def sample_host():
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    temps = []
    try:
        for chip, entries in psutil.sensors_temperatures().items():
            for e in entries:
                if e.current:
                    temps.append({"chip": chip, "label": e.label or chip, "c": round(e.current, 1)})
    except Exception:
        pass
    return {
        "cpu": psutil.cpu_percent(None), "cores": psutil.cpu_percent(percpu=True),
        "load": os.getloadavg(), "uptime": time.time() - psutil.boot_time(),
        "mem": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
                "cached": getattr(vm, "cached", 0)},
        "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
        "temps": temps,
        "disks": [disk_usage("/", "/")] + [disk_usage(m["label"], m["path"]) for m in MOUNTS],
        "net": sample_net(),
    }


def sample_raid(smart_disks):
    """None means "no md RAID here": the dashboard hides the section instead of showing an error."""
    md = HOST_PATHS["md"]
    if not RAID_DEVICE or not os.path.isdir(md):
        return None
    r = {"name": RAID_DEVICE}
    try:
        g = lambda k: read(f"{md}/{k}")
        r.update(array_state=g("array_state"), level=g("level"), raid_disks=int(g("raid_disks")),
                 degraded=int(g("degraded")), sync_action=g("sync_action"), mismatch=int(g("mismatch_cnt")))
        sc = g("sync_completed")
        r["sync_pct"] = None
        if "/" in sc:
            a, b = (int(x) for x in sc.split("/"))
            r["sync_pct"] = round(a / b * 100, 1) if b else None
        try:
            r["sync_speed_kbs"] = int(g("sync_speed"))
        except (OSError, ValueError):
            r["sync_speed_kbs"] = None
        by_dev = {d.get("dev"): d.get("id") for d in (smart_disks or [])}
        members = []
        for entry in sorted(os.listdir(md)):
            if entry.startswith("dev-"):
                dn = entry[4:]
                parent = dn if dn in by_dev else re.sub(r"p?\d+$", "", dn)
                members.append({"dev": dn, "state": read(f"{md}/{entry}/state"),
                                "slot": read(f"{md}/{entry}/slot"), "errors": int(read(f"{md}/{entry}/errors")),
                                "by_id": by_dev.get(parent)})
        r["members"] = members
    except OSError as e:
        r["error"] = f"cannot read {RAID_DEVICE} sysfs: {type(e).__name__}"
    try:
        txt = read(HOST_PATHS["mdstat"])
        m = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", txt)
        if m:
            r["mdstat_flags"] = m.group(3)
        m = re.search(r"finish=([\d.]+min)", txt)
        r["finish"] = m.group(1) if m else None
    except OSError:
        pass
    return r


def sample_smart():
    """Read the file the host collector writes. Never present old data as fresh: a missing, unreadable or
    stale file is reported as such (and a stale file is flagged, not hidden)."""
    path = HOST_PATHS["smart"]
    try:
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("disks"), list):
            raise ValueError("unexpected layout")
    except FileNotFoundError:
        return {"error": "No SMART data: smart.json not found. The host collector (dashboard-smart.timer) "
                         "is not installed, has not run yet, or the compose mount is wrong. See docs/smart-setup.md.", "disks": []}
    except (OSError, ValueError):
        return {"error": "SMART data file exists but cannot be read or parsed. Check its permissions and "
                         "`journalctl -u dashboard-smart.service`. See docs/smart-setup.md.", "disks": []}
    gen = d.get("generated")
    if not isinstance(gen, (int, float)):
        return {"error": "SMART data file has no timestamp, so its age is unknown; not showing it.", "disks": []}
    d["age"] = time.time() - gen
    d["stale"] = d["age"] > SMART_STALE_AFTER or d["age"] < -300
    d["stale_after"] = SMART_STALE_AFTER
    return d


def host_loop():
    psutil.cpu_percent(None); psutil.cpu_percent(percpu=True)
    while True:
        if time.time() - last_seen < IDLE_AFTER:
            try:
                STATE["host"] = sample_host()
                STATE["smart"] = sample_smart()
                STATE["raid"] = sample_raid(STATE["smart"].get("disks"))
                STATE["ts"] = time.time()
            except Exception as e:  # keep the sampler alive
                logging.exception("host sampling failed: %s", e)
        time.sleep(2)

# ---------------------------------------------------------------- docker
dock = docker.DockerClient(base_url=os.environ.get("DOCKER_HOST", "tcp://dashboard-socket-proxy:2375"),
                           version="1.44", timeout=20, max_pool_size=16)
pool = ThreadPoolExecutor(8)
BAD_EXIT_OK = {0, 137, 143}      # clean exit, or stopped by `docker stop`


def protection(name, labels, image):
    proj = labels.get("com.docker.compose.project", "")
    if name in SELF_NAMES or proj == SELF_PROJECT:
        return "self", "Part of the dashboard itself"
    hay = " ".join([name, image, proj, labels.get("com.docker.compose.service", "")])
    m = CONFIRM_RE.search(hay)
    if m:
        return "confirm", f"'{name}' looks like {m.group(0)}: stopping it could cut off DNS or remote access"
    return None, None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def calc_stats(st):
    try:
        cpu, pre = st["cpu_stats"], st["precpu_stats"]
        cd = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sd = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        n = cpu.get("online_cpus") or 1
        pct = cd / sd * n * 100 if sd > 0 and cd >= 0 else 0.0
        ms = st["memory_stats"]
        ss = ms.get("stats", {})
        mem = max(ms.get("usage", 0) - ss.get("inactive_file", ss.get("total_inactive_file", 0)), 0)
        return round(pct, 1), mem, ms.get("limit")
    except (KeyError, TypeError):
        return None, None, None


def one_container(c):
    api = dock.api
    try:
        i = api.inspect_container(c["Id"])
    except docker.errors.APIError:
        return None
    s, cfg, hc = i["State"], i["Config"], i["HostConfig"]
    labels = cfg.get("Labels") or {}
    name = i["Name"].lstrip("/")
    prot, why = protection(name, labels, cfg.get("Image", ""))
    rec = {"name": name, "id": i["Id"][:12], "image": cfg.get("Image"), "status": s["Status"],
           "health": (s.get("Health") or {}).get("Status"), "restarts": i.get("RestartCount", 0),
           "policy": (hc.get("RestartPolicy") or {}).get("Name") or "no",
           "project": labels.get("com.docker.compose.project") or "(no compose project)",
           "service": labels.get("com.docker.compose.service"),
           "exit_code": s.get("ExitCode"), "oom": bool(s.get("OOMKilled")),
           "protection": prot, "protection_reason": why, "cpu": None, "mem": None, "mem_limit": None}
    started = parse_ts(s.get("StartedAt", ""))
    rec["uptime"] = time.time() - started if s["Status"] == "running" and started else None
    rec["finished"] = parse_ts(s.get("FinishedAt", "")) if s["Status"] != "running" else None
    rec["unexpected"] = (s["Status"] in ("dead", "restarting") or rec["oom"] or
                         (s["Status"] == "exited" and s.get("ExitCode") not in BAD_EXIT_OK))
    if s["Status"] == "running":
        try:
            rec["cpu"], rec["mem"], rec["mem_limit"] = calc_stats(api.stats(c["Id"], stream=False))
        except Exception:
            pass
    return rec


def refresh_containers():
    try:
        lst = dock.api.containers(all=True)
        recs = [r for r in pool.map(one_container, lst) if r]
        STATE["containers"] = {"list": sorted(recs, key=lambda r: (r["project"], r["name"])), "ts": time.time()}
    except Exception as e:
        STATE["containers"] = {"error": f"Docker proxy unreachable: {type(e).__name__}", "list": [], "ts": time.time()}


def docker_loop():
    while True:
        if time.time() - last_seen < IDLE_AFTER:
            refresh_containers()
        time.sleep(3)

# ---------------------------------------------------------------- alerts
def alerts():
    out = []
    add = lambda lvl, key, txt: out.append({"level": lvl, "key": key, "text": txt})
    raid = STATE["raid"] or {}
    if not raid:
        pass
    elif raid.get("error"):
        add("warn", "raid", raid["error"])
    else:
        bad = [m["dev"] for m in raid.get("members", []) if "faulty" in m["state"]]
        if raid.get("degraded") or bad or "_" in (raid.get("mdstat_flags") or ""):
            add("crit", "raid", "RAID degraded" + (f" (faulty: {', '.join(bad)})" if bad else ""))
        if raid.get("sync_action") in ("resync", "recover"):
            add("warn", "raid", f"RAID {raid['sync_action']} in progress ({raid.get('sync_pct')}%)")
        elif raid.get("sync_action") in ("check", "repair"):
            add("info", "raid", f"RAID {raid['sync_action']} running ({raid.get('sync_pct')}%)")
        if raid.get("mismatch"):
            add("warn", "raid", f"RAID mismatch count {raid['mismatch']}")
    sm = STATE["smart"] or {}
    if sm.get("error"):
        add("warn", "smart", sm["error"])
    else:
        if sm.get("stale"):
            add("warn", "smart", f"SMART data is STALE ({int(sm['age'] // 60)} min old; collector timer stopped?). Values shown are not current.")
        for d in sm.get("disks", []):
            nm = d.get("label") or d.get("model") or d.get("id")
            if d.get("error"):
                add("warn", "smart", f"{nm}: {d['error']}")
            if d.get("passed") is False:
                add("crit", "smart", f"SMART FAILING: {nm}")
            for k, lbl in (("reallocated", "reallocated"), ("pending", "pending"), ("uncorrectable", "uncorrectable")):
                if d.get(k):
                    add("warn", "smart", f"{nm}: {d[k]} {lbl} sectors")
            if d.get("temp_c") and d["temp_c"] >= SMART_TEMP_WARN:
                add("warn", "smart", f"{nm} is {d['temp_c']} °C")
    h = STATE["host"] or {}
    for d in h.get("disks", []):
        if d.get("percent", 0) >= DISK_CRIT:
            add("crit", "disk", f"{d['label']} is {d['percent']}% full")
    if h:
        if h["mem"]["percent"] >= RAM_WARN:
            add("warn", "ram", f"RAM {h['mem']['percent']}% used")
        if h["swap"]["total"] and h["swap"]["percent"] >= SWAP_WARN:
            add("warn", "ram", f"Swap {h['swap']['percent']}% used")
    ct = STATE["containers"] or {}
    if ct.get("error"):
        add("warn", "docker", ct["error"])
    for c in ct.get("list", []):
        if c["unexpected"]:
            add("crit", "container", f"Container {c['name']} {c['status']}"
                + (f" (exit {c['exit_code']})" if c["status"] == "exited" else "") + (" OOM-killed" if c["oom"] else ""))
        elif c["health"] == "unhealthy":
            add("warn", "container", f"Container {c['name']} is unhealthy")
    return out

# ---------------------------------------------------------------- app
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
def _start():
    threading.Thread(target=host_loop, daemon=True).start()
    threading.Thread(target=docker_loop, daemon=True).start()


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.get("/api/info")
def info():
    return {"title": TITLE}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
async def login(request: Request):
    ip = client_ip(request)
    if (bad := check_post(request)):
        return bad
    wait = login_blocked(ip)
    if wait:
        audit("login_blocked", ip)
        r = deny(429, f"Too many attempts. Try again in {wait // 60 + 1} min.")
        r.headers["Retry-After"] = str(wait)
        return r
    try:
        pw = (await request.json()).get("password", "")
    except Exception:
        pw = ""
    if not isinstance(pw, str) or not pw or len(pw) > 256 or not verify_password(pw):
        now = time.time()
        FAILS.setdefault(ip, []).append(now)
        GLOBAL_FAILS.append(now)
        audit("login_fail", ip)
        time.sleep(0.5)
        return deny(401, "Wrong password")
    FAILS.pop(ip, None)
    sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with _slock:
        SESSIONS[sid] = {"csrf": csrf, "exp": time.time() + SESSION_TTL}
    audit("login_ok", ip)
    r = JSONResponse({"csrf": csrf})
    r.set_cookie("__Host-session", sid, max_age=SESSION_TTL, httponly=True, secure=True, samesite="strict", path="/")
    return r


@app.get("/api/session")
def session(request: Request):
    _, s = get_session(request)
    return {"csrf": s["csrf"]} if s else deny(401, "login required")


@app.post("/api/logout")
def logout(request: Request):
    sid, s = get_session(request)
    if not s:
        return deny(401, "login required")
    if (bad := check_post(request, s)):
        return bad
    with _slock:
        SESSIONS.pop(sid, None)
    audit("logout", client_ip(request))
    r = JSONResponse({"ok": True})
    r.delete_cookie("__Host-session", path="/", secure=True, httponly=True, samesite="strict")
    return r


@app.get("/api/state")
def state(request: Request):
    global last_seen
    _, s = get_session(request)
    if not s:
        return deny(401, "login required")
    last_seen = time.time()
    return {"now": time.time(), "hostname": TITLE,
            "host": STATE["host"], "raid": STATE["raid"], "smart": STATE["smart"],
            "containers": STATE["containers"], "alerts": alerts(), "audit": audit_tail()}


@app.get("/api/containers/{name}/logs")
def logs(name: str, request: Request):
    _, s = get_session(request)
    if not s:
        return deny(401, "login required")
    if not NAME_RE.match(name):
        return deny(400, "bad name")
    try:
        raw = dock.api.logs(name, stdout=True, stderr=True, tail=200, timestamps=True)
    except docker.errors.NotFound:
        return deny(404, "no such container")
    except Exception as e:
        return deny(502, f"docker error: {type(e).__name__}")
    return PlainTextResponse(raw.decode("utf-8", "replace")[-200_000:])


@app.post("/api/containers/{name}/{action}")
def act(name: str, action: str, request: Request, body: dict = Body(default={})):
    ip = client_ip(request)
    _, s = get_session(request)
    if not s:
        return deny(401, "login required")
    if (bad := check_post(request, s)):
        return bad
    if action not in ("start", "stop", "restart") or not NAME_RE.match(name):
        return deny(400, "bad request")
    confirmed = body.get("confirm") is True
    try:
        info = dock.api.inspect_container(name)
    except docker.errors.NotFound:
        return deny(404, "no such container")
    except Exception as e:
        return deny(502, f"docker error: {type(e).__name__}")
    real = info["Name"].lstrip("/")
    prot, why = protection(real, info["Config"].get("Labels") or {}, info["Config"].get("Image", ""))
    if prot == "self" and action != "start":
        audit(action, ip, container=real, result="blocked_self")
        return deny(403, f"{why}: refusing to {action} it from here")
    if prot == "confirm" and action != "start" and not confirmed:
        audit(action, ip, container=real, result="needs_confirm")
        return deny(409, why, needs_confirm=True)
    try:
        fn = {"start": dock.api.start, "stop": dock.api.stop, "restart": dock.api.restart}[action]
        fn(info["Id"]) if action == "start" else fn(info["Id"], timeout=15)
    except Exception as e:
        audit(action, ip, container=real, result="error", detail=type(e).__name__, confirmed=confirmed)
        return deny(502, f"docker refused: {e}")
    audit(action, ip, container=real, result="ok", confirmed=confirmed)
    refresh_containers()        # so the page shows the new state immediately
    return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
