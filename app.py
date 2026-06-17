"""
Pro VPS Panel - Railway deployable
Owner: shappno / shappno_codex
"""
import os, json, time, uuid, shutil, subprocess, threading, signal, secrets
from collections import deque
from pathlib import Path
from functools import wraps
from flask import (
    Flask, request, redirect, url_for, session,
    render_template, jsonify, Response, send_from_directory, abort
)
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
PRICING_FILE = DATA_DIR / "pricing.json"
FILES_ROOT = APP_DIR / "user_files"
DATA_DIR.mkdir(exist_ok=True)
FILES_ROOT.mkdir(exist_ok=True)

OWNER_USER = "YOUR_USERNAME"
OWNER_PASS = "YOUR_PASS"

DEFAULT_PRICING = {
    "currency": "₹",
    "contact": "Telegram: @shappno_044x",
    "plans": [
        {"name": "Starter", "duration": "24 Hours",  "price": "49",  "features": "1 file run, 512MB RAM, Real-time logs"},
        {"name": "Basic",   "duration": "7 Days",    "price": "199", "features": "Multi-file upload, pip/npm install, 24/7 uptime"},
        {"name": "Pro",     "duration": "30 Days",   "price": "599", "features": "Unlimited modules, Priority support, Auto-restart"},
        {"name": "Premium", "duration": "Lifetime",  "price": "1999","features": "All features, Custom domain, Dedicated help"},
    ],
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB upload

# ---------- storage ----------
_lock = threading.Lock()

def load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return {}

def save_users(u):
    with _lock:
        USERS_FILE.write_text(json.dumps(u, indent=2))

def load_pricing():
    if not PRICING_FILE.exists():
        save_pricing(DEFAULT_PRICING)
        return DEFAULT_PRICING
    try:
        return json.loads(PRICING_FILE.read_text())
    except Exception:
        return DEFAULT_PRICING

def save_pricing(p):
    with _lock:
        PRICING_FILE.write_text(json.dumps(p, indent=2))

def user_dir(username):
    d = FILES_ROOT / username
    d.mkdir(parents=True, exist_ok=True)
    return d

# ---------- process manager ----------
PROCS = {}

def _reader(username, proc):
    buf = PROCS[username]["logs"]
    try:
        for line in iter(proc.stdout.readline, b""):
            try:
                txt = line.decode("utf-8", errors="replace").rstrip()
            except Exception:
                txt = str(line)
            buf.append(f"[{time.strftime('%H:%M:%S')}] {txt}")
    except Exception as e:
        buf.append(f"[reader-error] {e}")
    finally:
        buf.append(f"[exit] process ended with code {proc.poll()}")

def start_process(username, filename):
    stop_process(username)
    udir = user_dir(username)
    fpath = udir / filename
    if not fpath.exists():
        return False, "File not found"
    ext = fpath.suffix.lower()
    if ext == ".py":
        cmd = ["python", "-u", str(fpath)]
    elif ext in (".js", ".mjs", ".cjs"):
        cmd = ["node", str(fpath)]
    elif ext == ".sh":
        cmd = ["bash", str(fpath)]
    else:
        return False, f"Unsupported file type: {ext}"
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(udir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1,
        )
    except FileNotFoundError as e:
        return False, f"Runtime not installed: {e}"
    logs = deque(maxlen=2000)
    logs.append(f"[start] {' '.join(cmd)}")
    PROCS[username] = {"proc": proc, "logs": logs, "file": filename}
    t = threading.Thread(target=_reader, args=(username, proc), daemon=True)
    t.start()
    PROCS[username]["thread"] = t
    return True, "started"

def stop_process(username):
    info = PROCS.get(username)
    if not info:
        return False
    p = info["proc"]
    if p.poll() is None:
        try:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        except Exception:
            pass
        info["logs"].append("[stop] process terminated")
    return True

def is_running(username):
    info = PROCS.get(username)
    return bool(info and info["proc"].poll() is None)

def get_logs(username):
    info = PROCS.get(username)
    if not info:
        return []
    return list(info["logs"])

# ---------- install module ----------
INSTALL_LOGS = {}

def run_install(username, command):
    parts = command.strip().split()
    if not parts:
        return False, "empty command"
    if parts[0] not in ("pip", "pip3", "npm"):
        return False, "Only 'pip install <pkg>' or 'npm install <pkg>' allowed"
    if len(parts) < 3 or parts[1] != "install":
        return False, "Format: pip install <module>  OR  npm install <module>"
    if any(c in command for c in [";", "&", "|", "`", "$(", ">"]):
        return False, "Invalid characters"
    logs = INSTALL_LOGS.setdefault(username, deque(maxlen=1000))
    logs.append(f"[install] $ {command}")
    cwd = str(user_dir(username))
    def worker():
        try:
            p = subprocess.Popen(parts, cwd=cwd,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in iter(p.stdout.readline, b""):
                logs.append(line.decode("utf-8", errors="replace").rstrip())
            p.wait()
            logs.append(f"[install] finished with code {p.returncode}")
        except Exception as e:
            logs.append(f"[install-error] {e}")
    threading.Thread(target=worker, daemon=True).start()
    return True, "installing"

# ---------- auth ----------
def is_owner():
    return session.get("role") == "owner"

def current_user():
    return session.get("username")

def user_valid(username):
    users = load_users()
    u = users.get(username)
    if not u:
        return False, "User not found"
    if u.get("expires_at") and time.time() > u["expires_at"]:
        del users[username]
        save_users(users)
        stop_process(username)
        return False, "Account expired"
    return True, u

def require_owner(f):
    @wraps(f)
    def w(*a, **kw):
        if not is_owner():
            return redirect(url_for("login"))
        return f(*a, **kw)
    return w

def require_user(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u or session.get("role") != "user":
            return redirect(url_for("login"))
        ok, _ = user_valid(u)
        if not ok:
            session.clear()
            return redirect(url_for("login"))
        return f(*a, **kw)
    return w

# ---------- routes ----------
@app.route("/")
def home():
    if is_owner():
        return redirect(url_for("owner_dashboard"))
    if current_user():
        return redirect(url_for("user_dashboard"))
    return redirect(url_for("landing"))

@app.route("/home")
def landing():
    return render_template("landing.html", pricing=load_pricing())

@app.route("/pricing")
def pricing_page():
    return render_template("pricing.html", pricing=load_pricing())

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u == OWNER_USER and p == OWNER_PASS:
            session.clear()
            session["role"] = "owner"
            session["username"] = u
            return redirect(url_for("owner_dashboard"))
        users = load_users()
        info = users.get(u)
        if info and info["password"] == p:
            ok, _ = user_valid(u)
            if not ok:
                error = "Account expired"
            else:
                session.clear()
                session["role"] = "user"
                session["username"] = u
                return redirect(url_for("user_dashboard"))
        else:
            error = "Invalid credentials"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))

@app.route("/auto/<token>")
def auto_login(token):
    users = load_users()
    for uname, info in users.items():
        if info.get("token") == token:
            ok, _ = user_valid(uname)
            if not ok:
                return "Account expired", 403
            session.clear()
            session["role"] = "user"
            session["username"] = uname
            return redirect(url_for("user_dashboard"))
    return "Invalid link", 404

# ---------- owner ----------
@app.route("/owner")
@require_owner
def owner_dashboard():
    users = load_users()
    now = time.time()
    changed = False
    for uname in list(users.keys()):
        if users[uname].get("expires_at") and now > users[uname]["expires_at"]:
            del users[uname]
            stop_process(uname)
            changed = True
    if changed:
        save_users(users)
    base = request.host_url.rstrip("/")
    return render_template("owner.html", users=users, now=now, base_url=base, pricing=load_pricing())

@app.route("/owner/create", methods=["POST"])
@require_owner
def owner_create():
    u = request.form.get("username", "").strip()
    p = request.form.get("password", "").strip()
    try:
        hours = float(request.form.get("hours", "24"))
    except ValueError:
        hours = 24
    if not u or not p:
        return redirect(url_for("owner_dashboard"))
    if u == OWNER_USER:
        return redirect(url_for("owner_dashboard"))
    users = load_users()
    users[u] = {
        "password": p,
        "created_at": time.time(),
        "expires_at": time.time() + hours * 3600 if hours > 0 else 0,
        "token": secrets.token_urlsafe(16),
    }
    save_users(users)
    user_dir(u)
    return redirect(url_for("owner_dashboard"))

@app.route("/owner/delete/<username>", methods=["POST"])
@require_owner
def owner_delete(username):
    users = load_users()
    if username in users:
        stop_process(username)
        del users[username]
        save_users(users)
        d = FILES_ROOT / username
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    return redirect(url_for("owner_dashboard"))

@app.route("/owner/extend/<username>", methods=["POST"])
@require_owner
def owner_extend(username):
    try:
        hours = float(request.form.get("hours", "24"))
    except ValueError:
        hours = 24
    users = load_users()
    if username in users:
        base = max(users[username].get("expires_at") or time.time(), time.time())
        users[username]["expires_at"] = base + hours * 3600
        save_users(users)
    return redirect(url_for("owner_dashboard"))

@app.route("/owner/pricing", methods=["POST"])
@require_owner
def owner_pricing():
    pricing = load_pricing()
    pricing["currency"] = request.form.get("currency", "₹").strip() or "₹"
    pricing["contact"] = request.form.get("contact", "").strip()
    plans = []
    names = request.form.getlist("p_name")
    durs = request.form.getlist("p_duration")
    prices = request.form.getlist("p_price")
    feats = request.form.getlist("p_features")
    for i in range(len(names)):
        if not names[i].strip():
            continue
        plans.append({
            "name": names[i].strip(),
            "duration": durs[i].strip() if i < len(durs) else "",
            "price": prices[i].strip() if i < len(prices) else "0",
            "features": feats[i].strip() if i < len(feats) else "",
        })
    pricing["plans"] = plans
    save_pricing(pricing)
    return redirect(url_for("owner_dashboard") + "#pricing")

# ---------- user dashboard ----------
@app.route("/dashboard")
@require_user
def user_dashboard():
    u = current_user()
    users = load_users()
    info = users.get(u, {})
    udir = user_dir(u)
    files = sorted([f.name for f in udir.iterdir() if f.is_file()])
    return render_template("user.html",
        username=u, info=info, files=files,
        running=is_running(u),
        running_file=(PROCS.get(u, {}).get("file") if is_running(u) else None),
        expires_at=info.get("expires_at", 0),
        now=time.time(),
    )

@app.route("/upload", methods=["POST"])
@require_user
def upload():
    u = current_user()
    udir = user_dir(u)
    files = request.files.getlist("files")
    for f in files:
        if not f or not f.filename:
            continue
        name = secure_filename(f.filename)
        if not name:
            continue
        f.save(udir / name)
    return redirect(url_for("user_dashboard"))

@app.route("/file/delete/<name>", methods=["POST"])
@require_user
def file_delete(name):
    u = current_user()
    name = secure_filename(name)
    p = user_dir(u) / name
    if p.exists() and p.is_file():
        p.unlink()
    return redirect(url_for("user_dashboard"))

@app.route("/file/view/<name>")
@require_user
def file_view(name):
    u = current_user()
    name = secure_filename(name)
    return send_from_directory(user_dir(u), name, as_attachment=False)

@app.route("/server/start", methods=["POST"])
@require_user
def server_start():
    u = current_user()
    fname = secure_filename(request.form.get("file", ""))
    ok, msg = start_process(u, fname)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/stop", methods=["POST"])
@require_user
def server_stop():
    u = current_user()
    stop_process(u)
    return jsonify({"ok": True})

@app.route("/server/restart", methods=["POST"])
@require_user
def server_restart():
    u = current_user()
    info = PROCS.get(u)
    fname = info["file"] if info else secure_filename(request.form.get("file", ""))
    if not fname:
        return jsonify({"ok": False, "msg": "no file"})
    stop_process(u)
    time.sleep(0.3)
    ok, msg = start_process(u, fname)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/delete", methods=["POST"])
@require_user
def server_delete():
    u = current_user()
    stop_process(u)
    PROCS.pop(u, None)
    return jsonify({"ok": True})

@app.route("/logs")
@require_user
def logs_api():
    u = current_user()
    return jsonify({
        "running": is_running(u),
        "file": PROCS.get(u, {}).get("file"),
        "logs": get_logs(u),
        "install": list(INSTALL_LOGS.get(u, [])),
    })

@app.route("/install", methods=["POST"])
@require_user
def install():
    u = current_user()
    cmd = request.form.get("command", "").strip()
    ok, msg = run_install(u, cmd)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/healthz")
def health():
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
