"""
개인 계정 로그인 게이트 (2026-10-23 사내 계정 통일)

- 계정 파일: $DATA_DIR/users.json (비번은 werkzeug 해시)
  DATA_DIR 미설정(= Railway Volume 없음)이면 앱 폴더/data 에 저장 → 재배포 때 사라짐.
  그 경우 매 부팅 SEED_USERS 로 다시 생성되므로, 바꾼 비번은 재배포 시 초기화된다(화면에 안내).
- SEED_USERS="이름:비번:admin,이름:비번,..." → 없는 이름만 생성, 기존 계정은 건드리지 않음.
- 같은 아이디 10회 연속 실패 → 5분 잠금 (메모리).
"""
import json
import os
import secrets
import threading
import time
import unicodedata
from pathlib import Path

from flask import (Blueprint, abort, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
PERSISTENT = bool(os.environ.get("DATA_DIR", "").strip())
DATA_DIR = Path(os.environ.get("DATA_DIR", "").strip() or (BASE_DIR / "data"))
USERS_FILE = DATA_DIR / "users.json"

MAX_FAILS = 10
LOCK_SECONDS = 5 * 60
OPEN_PATHS = ("/login", "/health", "/static")

bp = Blueprint("auth", __name__)
_file_lock = threading.RLock()


def norm(name: str) -> str:
    return unicodedata.normalize("NFC", (name or "").strip())


# ── 저장소 ─────────────────────────────────────────────────────────────
def _load() -> dict:
    with _file_lock:
        try:
            data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault("users", {})
        data.setdefault("meta", {})
        return data


def _save(data: dict):
    with _file_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = USERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, USERS_FILE)


def parse_seed(raw: str | None = None):
    raw = os.environ.get("SEED_USERS", "") if raw is None else raw
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 2:
            continue
        name = norm(parts[0])
        is_admin = len(parts) >= 3 and parts[-1].strip().lower() == "admin"
        pw = ":".join(parts[1:-1] if is_admin else parts[1:])
        if name and pw:
            out.append((name, pw, is_admin))
    return out


def seed_users():
    seeds = parse_seed()
    with _file_lock:
        data = _load()
        users = data["users"]
        changed = False
        migrated = data["meta"].get("auth_v2_migrated")
        for name, pw, is_admin in seeds:
            if name not in users:
                users[name] = {"pw": generate_password_hash(pw), "admin": is_admin}
                changed = True
            elif not migrated:
                users[name] = {"pw": generate_password_hash(pw), "admin": is_admin}
                changed = True
        if seeds and not migrated:
            data["meta"]["auth_v2_migrated"] = True
            changed = True
        if changed:
            _save(data)


def _secret_key() -> str:
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    f = DATA_DIR / "secret_key.txt"
    try:
        return f.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = secrets.token_hex(32)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(key, encoding="utf-8")
        return key


# ── 시도 제한 ───────────────────────────────────────────────────────────
_lock = threading.Lock()
_fails: dict = {}


def _locked_remaining(name: str) -> int:
    with _lock:
        st = _fails.get(name)
        if not st or not st.get("until"):
            return 0
        left = int(st["until"] - time.time())
        if left <= 0:
            _fails.pop(name, None)
            return 0
        return left


def _record_failure(name: str) -> int:
    with _lock:
        st = _fails.setdefault(name, {"count": 0, "until": 0})
        st["count"] += 1
        if st["count"] >= MAX_FAILS:
            st["count"] = 0
            st["until"] = time.time() + LOCK_SECONDS
            return 0
        return MAX_FAILS - st["count"]


def _record_success(name: str):
    with _lock:
        _fails.pop(name, None)


# ── 게이트 ─────────────────────────────────────────────────────────────
def _current_user():
    name = session.get("user")
    if not name:
        return None
    u = _load()["users"].get(name)
    return (name, u) if u else None


def init_auth(app):
    app.secret_key = _secret_key()
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", 60 * 60 * 24 * 30)
    seed_users()
    app.register_blueprint(bp)

    @app.before_request
    def _require_login():
        if request.path.startswith(OPEN_PATHS):
            return None
        cu = _current_user()
        if cu:
            session["is_admin"] = bool(cu[1].get("admin"))
            return None
        session.clear()
        if request.method == "GET" and request.path == "/":
            return redirect(url_for("auth.login"))
        if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
            return redirect(url_for("auth.login", next=request.path))
        return jsonify({"error": "로그인이 필요합니다."}), 401

    @app.context_processor
    def _inject():
        return {"auth_user": session.get("user"), "auth_is_admin": session.get("is_admin"),
                "auth_persistent": PERSISTENT}


def _safe_next(nxt):
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


@bp.get("/health")
def health():
    return {"ok": True}


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    no_accounts = not _load()["users"]
    if request.method == "POST":
        u = norm(request.form.get("username", ""))
        p = request.form.get("password", "")
        left = _locked_remaining(u)
        if left:
            error = f"로그인 시도가 너무 많아요. {max(1, (left + 59) // 60)}분 후 다시 시도하세요."
        else:
            row = _load()["users"].get(u) if u else None
            if row and check_password_hash(row["pw"], p):
                _record_success(u)
                session.clear()
                session["user"] = u
                session["is_admin"] = bool(row.get("admin"))
                session.permanent = True
                return redirect(_safe_next(request.args.get("next")))
            remain = _record_failure(u)
            if remain == 0:
                error = f"아이디 또는 비밀번호가 틀렸어요. {LOCK_SECONDS // 60}분 후 다시 시도하세요."
            else:
                error = f"아이디 또는 비밀번호가 틀렸어요. (남은 시도 {remain}회)"
    return render_template("login.html", error=error, no_accounts=no_accounts)


@bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/password", methods=["GET", "POST"])
def change_password():
    error = ok = None
    if request.method == "POST":
        name = session.get("user")
        cur = request.form.get("current", "")
        new = request.form.get("new", "")
        new2 = request.form.get("new2", "")
        with _file_lock:
            data = _load()
            row = data["users"].get(name)
            if not row or not check_password_hash(row["pw"], cur):
                error = "현재 비밀번호가 틀렸어요."
            elif len(new) < 4:
                error = "새 비밀번호는 4자 이상이어야 해요."
            elif new != new2:
                error = "새 비밀번호 확인이 일치하지 않아요."
            else:
                row["pw"] = generate_password_hash(new)
                _save(data)
                ok = "비밀번호가 변경되었습니다."
    return render_template("password.html", error=error, ok=ok)


# ── 관리자: 계정 관리 ──────────────────────────────────────────────────
def _admin_only():
    if not session.get("is_admin"):
        abort(403)


@bp.route("/admin/users", methods=["GET", "POST"])
def admin_users():
    _admin_only()
    msg = err = None
    if request.method == "POST":
        action = request.form.get("action")
        name = norm(request.form.get("name", ""))
        pw = request.form.get("password", "")
        with _file_lock:
            data = _load()
            users = data["users"]
            if action == "add":
                if not name or len(pw) < 4:
                    err = "이름과 초기 비밀번호(4자 이상)를 입력하세요."
                elif name in users:
                    err = "이미 있는 아이디입니다."
                else:
                    users[name] = {"pw": generate_password_hash(pw),
                                   "admin": request.form.get("admin") == "1"}
                    _save(data)
                    msg = f"'{name}' 계정을 추가했습니다."
            elif action == "reset":
                if name not in users:
                    err = "없는 계정입니다."
                elif len(pw) < 4:
                    err = "새 비밀번호는 4자 이상이어야 합니다."
                else:
                    users[name]["pw"] = generate_password_hash(pw)
                    _save(data)
                    msg = f"'{name}' 비밀번호를 재설정했습니다."
            elif action == "delete":
                if name not in users:
                    err = "없는 계정입니다."
                elif name == session.get("user"):
                    err = "본인 계정은 삭제할 수 없습니다."
                elif users[name].get("admin") and sum(1 for v in users.values() if v.get("admin")) <= 1:
                    err = "마지막 관리자 계정은 삭제할 수 없습니다."
                else:
                    users.pop(name)
                    _save(data)
                    msg = f"'{name}' 계정을 삭제했습니다."
            else:
                err = "알 수 없는 요청입니다."
    users = _load()["users"]
    rows = sorted(users.items(), key=lambda kv: (not kv[1].get("admin"), kv[0]))
    return render_template("admin_users.html", rows=rows, msg=msg, err=err)
