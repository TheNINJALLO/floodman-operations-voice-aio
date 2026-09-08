from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from app.db import Database


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
PASSWORD_SCHEME = "scrypt"
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LENGTH = 32


def normalize_username(value: str) -> str:
    return str(value or "").strip().lower()


def validate_username(value: str) -> str:
    username = normalize_username(value)
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 3-40 characters and use lowercase letters, numbers, dots, dashes, or underscores."
        )
    return username


def validate_password(value: str) -> str:
    password = str(value or "")
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    if len(password) > 256:
        raise ValueError("Password must be 256 characters or fewer.")
    groups = sum(
        bool(pattern.search(password))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"), re.compile(r"[^A-Za-z0-9]"))
    )
    if groups < 3:
        raise ValueError("Password must include at least three of: lowercase, uppercase, number, or symbol.")
    return password


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    password = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LENGTH,
        maxmem=128 * 1024 * 1024,
    )
    return f"{PASSWORD_SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = str(encoded or "").split("$", 5)
        if scheme != PASSWORD_SCHEME:
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_decode(expected)),
            maxmem=128 * 1024 * 1024,
        )
        return hmac.compare_digest(digest, _decode(expected))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int | None
    username: str
    display_name: str
    role: str
    csrf_token: str
    is_recovery: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_manage(self) -> bool:
        return self.role in {"admin", "manager"}


class AuthManager:
    def __init__(self, database: Database, recovery_token: str, session_hours: int = 12):
        self.database = database
        self.recovery_token = recovery_token
        self.session_hours = max(1, min(int(session_hours), 168))

    def authenticate(self, username: str, password: str) -> tuple[str, str] | None:
        user = self.database.get_user_by_username(normalize_username(username), include_secret=True)
        if not user or not user["active"] or not verify_password(password, user["password_hash"]):
            return None
        self.database.record_user_login(int(user["id"]))
        return self.database.create_session(int(user["id"]), False, self.session_hours)

    def authenticate_recovery(self, supplied: str) -> tuple[str, str] | None:
        value = str(supplied or "")
        if not value or not hmac.compare_digest(value, self.recovery_token):
            return None
        return self.database.create_session(None, True, min(self.session_hours, 2))

    def principal(self, token: str | None) -> Principal | None:
        if not token:
            return None
        record = self.database.get_session(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if not record:
            return None
        if record["is_recovery"]:
            return Principal(None, "recovery-admin", "Recovery administrator", "admin", record["csrf_token"], True)
        return Principal(
            int(record["user_id"]),
            record["username"],
            record["display_name"],
            record["role"],
            record["csrf_token"],
        )

    def logout(self, token: str | None) -> None:
        if token:
            self.database.delete_session(hashlib.sha256(token.encode("utf-8")).hexdigest())

    @staticmethod
    def require_csrf(principal: Principal, supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(principal.csrf_token, supplied):
            raise ValueError("Your session security token expired. Refresh the page and try again.")

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str,
        preferences: dict[str, bool],
        actor: Principal,
    ) -> int:
        if not actor.is_admin:
            raise PermissionError("Administrator access is required.")
        return self.database.create_user(
            validate_username(username),
            str(display_name or "").strip()[:80] or validate_username(username),
            hash_password(password),
            self._validate_role(role),
            preferences,
            actor.user_id,
        )

    def update_user(
        self,
        user_id: int,
        *,
        username: str,
        display_name: str,
        role: str,
        active: bool,
        preferences: dict[str, bool],
        actor: Principal,
    ) -> None:
        if not actor.is_admin:
            raise PermissionError("Administrator access is required.")
        current = self.database.get_user(user_id)
        if not current:
            raise LookupError("User not found.")
        next_role = self._validate_role(role)
        if current["role"] == "admin" and current["active"] and (not active or next_role != "admin"):
            if self.database.active_admin_count() <= 1:
                raise ValueError("At least one active administrator must remain.")
        if actor.user_id == user_id and not active:
            raise ValueError("You cannot deactivate your own account.")
        self.database.update_user(
            user_id,
            validate_username(username),
            str(display_name or "").strip()[:80] or validate_username(username),
            next_role,
            active,
            preferences,
        )

    def set_password(self, user_id: int, password: str, actor: Principal) -> None:
        if not actor.is_admin and actor.user_id != user_id:
            raise PermissionError("You can only change your own password.")
        if not self.database.get_user(user_id):
            raise LookupError("User not found.")
        self.database.update_user_password(user_id, hash_password(password))

    def delete_user(self, user_id: int, actor: Principal) -> None:
        if not actor.is_admin:
            raise PermissionError("Administrator access is required.")
        if actor.user_id == user_id:
            raise ValueError("You cannot delete your own account.")
        current = self.database.get_user(user_id)
        if not current:
            raise LookupError("User not found.")
        if current["role"] == "admin" and current["active"] and self.database.active_admin_count() <= 1:
            raise ValueError("At least one active administrator must remain.")
        self.database.delete_user(user_id)

    @staticmethod
    def _validate_role(role: str) -> str:
        value = str(role or "").strip().lower()
        if value not in {"admin", "manager", "viewer"}:
            raise ValueError("Role must be admin, manager, or viewer.")
        return value
