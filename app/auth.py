"""Username/password auth with signed session cookies (itsdangerous) —
no external session store needed."""

import secrets

import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import appkey, db

COOKIE_NAME = "erm_session"
MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def _serializer():
    return URLSafeTimedSerializer(appkey.get_key(), salt="erm-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))


def create_user(username: str, password: str, is_admin: bool = False, manager_id: str = "", email: str = ""):
    with db.get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise ValueError("Username already taken.")
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, email) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), 1 if is_admin else 0, (email or "").strip()),
        )
        user_id = cur.lastrowid
        conn.execute(
            "INSERT INTO user_prefs (user_id, manager_id) VALUES (?, ?)", (user_id, manager_id.strip())
        )
        return user_id


def seed_admin_if_missing(username: str = "admin"):
    """Creates the admin account on first startup if no admin exists yet.
    Returns the generated password if it just created one, or None if an
    admin account already existed (nothing to show/log in that case)."""
    with db.get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE is_admin = 1").fetchone()
        if existing:
            return None
    password = secrets.token_urlsafe(15)
    try:
        create_user(username, password, is_admin=True)
    except ValueError:
        # Username taken by a non-admin account — promote it instead of
        # failing startup outright, rather than silently having no admin.
        with db.get_db() as conn:
            conn.execute("UPDATE users SET is_admin = 1 WHERE username = ?", (username,))
        return None
    return password


def authenticate(username: str, password: str):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return dict(row)


def make_session_cookie(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session_cookie(value: str):
    if not value:
        return None
    try:
        data = _serializer().loads(value, max_age=MAX_AGE_SECONDS)
    except BadSignature:
        return None
    return data.get("uid")


def get_user(user_id: int):
    with db.get_db() as conn:
        row = conn.execute("SELECT id, username, is_admin, created_on, email FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        user = dict(row)
        user["is_admin"] = bool(user["is_admin"])
        return user


def set_email(user_id: int, email: str):
    with db.get_db() as conn:
        conn.execute("UPDATE users SET email = ? WHERE id = ?", ((email or "").strip(), user_id))


def change_password(user_id: int, current_password: str, new_password: str):
    """Verify the current password, then set the new one. Raises ValueError
    with a user-facing message on failure."""
    if len(new_password or "") < 8:
        raise ValueError("New password must be at least 8 characters.")
    with db.get_db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not verify_password(current_password, row["password_hash"]):
        raise ValueError("Current password is incorrect.")
    set_password(user_id, new_password)


def list_users():
    """Includes each user's assigned Project Manager id (user_prefs.manager_id,
    admin-assigned — see /admin/users) — left blank ('') for an admin or for
    a regular user nobody has restricted yet, in which case they see every
    project (see main.py's _visible_projects)."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.is_admin, u.created_on, u.email, COALESCE(p.manager_id, '') AS manager_id
            FROM users u
            LEFT JOIN user_prefs p ON p.user_id = u.id
            ORDER BY u.is_admin DESC, u.username COLLATE NOCASE
            """
        ).fetchall()
        users = [dict(r) for r in rows]
        for u in users:
            u["is_admin"] = bool(u["is_admin"])
        return users


def set_password(user_id: int, password: str):
    with db.get_db() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))


def delete_user(user_id: int):
    """Refuses to delete the last remaining admin account, to avoid
    locking everyone out of App Settings/Users/Log."""
    with db.get_db() as conn:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError("User not found.")
        if row["is_admin"]:
            admin_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]
            if admin_count <= 1:
                raise ValueError("Cannot delete the last remaining admin account.")
        conn.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
