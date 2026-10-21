"""
Accounts for Clip Studio: one admin (the PC owner) plus invited users.

- First visit: create the admin account (only possible while no account exists).
- Admin creates single-use invite links; invited people pick a name + password.
- Passwords are stored as salted hashes (werkzeug / scrypt or pbkdf2).
- Sessions are signed cookies (HttpOnly, SameSite=Lax), valid for 30 days.
- Every state-changing request must carry the header "X-CS: 1" (CSRF protection:
  other websites can't add custom headers to cross-site requests).
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import abort, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

NAME_RE = re.compile(r"^[A-Za-z0-9_.\-äöüÄÖÜß]{2,32}$")
_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


class Accounts:
    def __init__(self, base: Path):
        self.file = base / "users.json"
        self.secret_file = base / ".secret_key"

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict:
        if self.file.exists():
            try:
                return json.loads(self.file.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"users": {}, "invites": {}}

    def _save(self, data: dict) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file)

    def secret_key(self) -> bytes:
        if not self.secret_file.exists():
            self.secret_file.write_bytes(secrets.token_bytes(32))
        return self.secret_file.read_bytes()

    # ------------------------------------------------------------------ users
    def has_users(self) -> bool:
        return bool(self._load()["users"])

    def get(self, name: str) -> dict | None:
        u = self._load()["users"].get(name)
        return {**u, "name": name} if u else None

    def list(self) -> list[dict]:
        d = self._load()
        return [{"name": n, "role": u["role"], "created": u.get("created"),
                 "last_login": u.get("last_login")} for n, u in sorted(d["users"].items())]

    def create(self, name: str, password: str, role: str = "user") -> str | None:
        """Returns an error message or None."""
        if not NAME_RE.match(name or ""):
            return "Name: 2–32 Zeichen, nur Buchstaben, Zahlen, . _ -"
        if len(password or "") < 8:
            return "Passwort muss mindestens 8 Zeichen haben"
        with _lock:
            d = self._load()
            if any(n.lower() == name.lower() for n in d["users"]):
                return "Name ist schon vergeben"
            d["users"][name] = {"pw": generate_password_hash(password), "role": role,
                                "created": time.time()}
            self._save(d)
        return None

    def check(self, name: str, password: str) -> dict | None:
        d = self._load()
        real = next((n for n in d["users"] if n.lower() == (name or "").lower()), None)
        if not real or not check_password_hash(d["users"][real]["pw"], password or ""):
            return None
        with _lock:
            d = self._load()
            d["users"][real]["last_login"] = time.time()
            self._save(d)
        return self.get(real)

    def set_password(self, name: str, password: str) -> str | None:
        if len(password or "") < 8:
            return "Passwort muss mindestens 8 Zeichen haben"
        with _lock:
            d = self._load()
            if name not in d["users"]:
                return "Unbekannter Benutzer"
            d["users"][name]["pw"] = generate_password_hash(password)
            d["users"][name]["pw_changed"] = time.time()
            self._save(d)
        return None

    def delete(self, name: str) -> None:
        with _lock:
            d = self._load()
            d["users"].pop(name, None)
            self._save(d)

    # ------------------------------------------------------------------ invites
    def new_invite(self, by: str, role: str = "user", days: int = 7) -> str:
        code = secrets.token_urlsafe(16)
        with _lock:
            d = self._load()
            d["invites"][code] = {"by": by, "role": role, "created": time.time(),
                                  "expires": time.time() + days * 86400}
            self._save(d)
        return code

    def invites(self) -> list[dict]:
        now = time.time()
        return [{"code": c, **i} for c, i in self._load()["invites"].items() if i["expires"] > now]

    def drop_invite(self, code: str) -> None:
        with _lock:
            d = self._load()
            d["invites"].pop(code, None)
            self._save(d)

    def redeem(self, code: str, name: str, password: str) -> str | None:
        inv = self._load()["invites"].get(code or "")
        if not inv or inv["expires"] < time.time():
            return "Einladung ungültig oder abgelaufen"
        err = self.create(name, password, inv.get("role", "user"))
        if not err:
            self.drop_invite(code)
        return err


# ---------------------------------------------------------------------- flask glue
def client_ip() -> str:
    return request.remote_addr or "?"


def too_many_failures() -> bool:
    now = time.time()
    lst = [t for t in _fails.get(client_ip(), []) if now - t < 900]
    _fails[client_ip()] = lst
    return len(lst) >= 10


def note_failure() -> None:
    _fails.setdefault(client_ip(), []).append(time.time())


def install(app, accounts: Accounts, public_paths: set[str], admin_prefixes: tuple[str, ...]):
    app.secret_key = accounts.secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_NAME="clipstudio", PERMANENT_SESSION_LIFETIME=timedelta(days=30))

    def current_user() -> dict | None:
        name = session.get("u")
        if not name:
            return None
        u = accounts.get(name)
        # invalidate sessions after a password change or account deletion
        if not u or session.get("pwc", 0) < u.get("pw_changed", 0):
            session.clear()
            return None
        return u

    app.current_user = current_user

    @app.before_request
    def _guard():
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-CS") != "1":
            return jsonify({"error": "Ungültige Anfrage"}), 403
        path = request.path
        if path in public_paths or path.startswith("/static/"):
            return None
        u = current_user()
        if not u:
            return jsonify({"error": "Nicht angemeldet", "login": True}), 401
        request.user = u
        if path.startswith(admin_prefixes) and u["role"] != "admin":
            return jsonify({"error": "Nur für Admins"}), 403
        return None

    def _login(u: dict):
        session.clear()
        session.permanent = True
        session["u"] = u["name"]
        session["pwc"] = time.time()

    @app.get("/api/me")
    def _me():
        u = current_user()
        if not u:
            return jsonify({"user": None, "setup": not accounts.has_users()}), 200
        return jsonify({"user": {"name": u["name"], "role": u["role"]}, "setup": False})

    @app.post("/api/setup")
    def _setup():
        if accounts.has_users():
            abort(403)
        d = request.get_json(force=True) or {}
        err = accounts.create(d.get("name", "").strip(), d.get("password", ""), "admin")
        if err:
            return jsonify({"error": err}), 400
        _login(accounts.get(d["name"].strip()))
        return _me()

    @app.post("/api/login")
    def _login_route():
        if too_many_failures():
            return jsonify({"error": "Zu viele Fehlversuche – bitte 15 Minuten warten"}), 429
        d = request.get_json(force=True) or {}
        u = accounts.check(d.get("name", "").strip(), d.get("password", ""))
        if not u:
            note_failure()
            time.sleep(0.7)
            return jsonify({"error": "Name oder Passwort falsch"}), 401
        _login(u)
        return _me()

    @app.post("/api/register")
    def _register():
        if too_many_failures():
            return jsonify({"error": "Zu viele Fehlversuche – bitte 15 Minuten warten"}), 429
        d = request.get_json(force=True) or {}
        name = d.get("name", "").strip()
        err = accounts.redeem(d.get("code", ""), name, d.get("password", ""))
        if err:
            note_failure()
            return jsonify({"error": err}), 400
        _login(accounts.get(name))
        return _me()

    @app.post("/api/logout")
    def _logout():
        session.clear()
        return jsonify({"ok": True})

    @app.post("/api/me/password")
    def _pw():
        u = request.user
        d = request.get_json(force=True) or {}
        if not accounts.check(u["name"], d.get("old", "")):
            return jsonify({"error": "Aktuelles Passwort falsch"}), 400
        err = accounts.set_password(u["name"], d.get("new", ""))
        if err:
            return jsonify({"error": err}), 400
        _login(accounts.get(u["name"]))
        return jsonify({"ok": True})

    @app.get("/api/admin/users")
    def _users():
        return jsonify({"users": accounts.list(), "invites": accounts.invites()})

    @app.post("/api/admin/invites")
    def _invite():
        role = (request.get_json(silent=True) or {}).get("role", "user")
        code = accounts.new_invite(request.user["name"], "admin" if role == "admin" else "user")
        return jsonify({"code": code})

    @app.delete("/api/admin/invites/<code>")
    def _drop_invite(code):
        accounts.drop_invite(code)
        return jsonify({"ok": True})

    @app.delete("/api/admin/users/<name>")
    def _del_user(name):
        if name == request.user["name"]:
            return jsonify({"error": "Du kannst dich nicht selbst löschen"}), 400
        accounts.delete(name)
        return jsonify({"ok": True})

    @app.post("/api/admin/users/<name>/password")
    def _reset_pw(name):
        err = accounts.set_password(name, (request.get_json(force=True) or {}).get("password", ""))
        if err:
            return jsonify({"error": err}), 400
        return jsonify({"ok": True})

    def admin_only(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if request.user["role"] != "admin":
                abort(403)
            return fn(*a, **kw)
        return wrapper

    return admin_only
