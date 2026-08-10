"""Supported collective-memory exchange boundary.

This module deliberately exposes two capabilities that do not share authority
or state:

* an immutable, provenance-complete export cache for accepted corpus
  generations; and
* a reviewed publication transaction that resolves logical targets internally,
  rebuilds the served projections, and commits a content-addressed receipt.

Contract documents contain logical IDs and hashes only.  Host paths, SQLite
handles, credentials, commands, and implementation exceptions never cross the
boundary.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

EXPORT_CATALOG_SCHEMA = "collective-export-catalog/v1"
EXPORT_MANIFEST_SCHEMA = "collective-export-manifest/v1"
EXPORT_PAGE_SCHEMA = "collective-export-page/v1"
PUBLICATION_DRAFT_SCHEMA = "collective-publication-draft/v1"
PUBLICATION_PREVIEW_SCHEMA = "collective-publication-preview/v1"
PUBLICATION_REQUEST_SCHEMA = "collective-publication-request/v1"
PUBLICATION_PLAN_SCHEMA = "collective-publication-plan/v1"
PUBLICATION_EVIDENCE_SCHEMA = "collective-publication-evidence/v1"
PUBLICATION_RECEIPT_SCHEMA = "collective-publication-receipt/v1"
PUBLICATION_STATE_SCHEMA = "collective-publication-state/v1"
CAPABILITY_SCHEMA = "collective-exchange-capability/v1"
TRUST_SCHEMA = "collective-exchange-trust/v1"
CONFIG_SCHEMA = "collective-exchange-config/v1"

MAX_ARTIFACTS = 4096
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_PUBLICATION_BYTES = 1024 * 1024
MAX_SOURCE_REFS = 128
MAX_PAGE = 256
MAX_SAFE_INTEGER = 9_007_199_254_740_991

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
B64U_SHA256_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
B64U_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(rb"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}\b", re.IGNORECASE),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        rb"\b(password|passwd|api_key|apikey|token)\b\s*[:=]\s*['\"]?[^'\"\s]{8,}",
        re.IGNORECASE,
    ),
    re.compile(rb"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@[^/\s]+", re.IGNORECASE),
)


class ExchangeError(RuntimeError):
    """Closed boundary error: stable code and non-sensitive public message."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "collective-exchange-error/v1",
            "code": self.code,
            "message": self.public_message,
        }


def _fail(code: str, message: str) -> NoReturn:
    raise ExchangeError(code, message)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or len(value) > 16 * 1024 * 1024
        or not B64U_RE.fullmatch(value)
        or len(value) % 4 == 1
    ):
        _fail("invalid_base64", "invalid bounded base64url value")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        _fail("invalid_base64", "invalid base64url value")
    if _b64u(decoded) != value:
        _fail("invalid_base64", "base64url value is not canonical")
    return decoded


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_json_value(value: Any, depth: int = 0) -> None:
    if depth > 64:
        _fail("json_too_deep", "JSON nesting exceeds the contract bound")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            _fail("non_canonical_number", "integer exceeds the interoperable bound")
        return
    if isinstance(value, float):
        _fail("non_canonical_number", "floating-point JSON values are forbidden")
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            _fail("invalid_json_key", "JSON object keys must be strings")
        for item in value.values():
            _validate_json_value(item, depth + 1)
        return
    _fail("invalid_json_type", "unsupported JSON value")


def canonical_bytes(value: Any) -> bytes:
    """Deterministic UTF-8 JSON for hashes and Ed25519 signatures.

    Exchange contracts intentionally avoid floating point values, so the
    sorted/minified representation is byte-stable across supported runtimes.
    """

    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_object_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            _fail("duplicate_json_key", "duplicate JSON object key")
        out[key] = value
    return out


def load_json_bytes(data: bytes, *, max_bytes: int = MAX_EXPORT_BYTES) -> Any:
    if len(data) > max_bytes:
        _fail("input_too_large", "JSON input exceeds the contract bound")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("invalid_utf8", "JSON input must be UTF-8")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_float=lambda _value: _fail(
                "non_canonical_number", "floating-point JSON values are forbidden"
            ),
            parse_constant=lambda _value: _fail(
                "non_canonical_number", "non-finite JSON values are forbidden"
            ),
        )
    except ExchangeError:
        raise
    except (json.JSONDecodeError, ValueError):
        _fail("invalid_json", "input is not valid closed JSON")
    _validate_json_value(value)
    return value


def load_json_file(
    path: str | os.PathLike[str], *, max_bytes: int = MAX_EXPORT_BYTES
) -> Any:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("input_unavailable", "configured input is unavailable")
    try:
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode):
            _fail("unsafe_input", "configured input must be a regular non-symlink file")
        if st.st_size > max_bytes:
            _fail("input_too_large", "configured input exceeds the contract bound")
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(max_bytes + 1)
        except OSError:
            _fail("input_unavailable", "configured input could not be read")
        return load_json_bytes(data, max_bytes=max_bytes)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _closed(
    value: Any,
    *,
    required: Sequence[str],
    optional: Sequence[str] = (),
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("invalid_contract", f"{name} must be an object")
    allowed = set(required) | set(optional)
    unknown = sorted(set(value) - allowed)
    missing = sorted(set(required) - set(value))
    if unknown:
        _fail("unknown_field", f"{name} contains unknown fields")
    if missing:
        _fail("missing_field", f"{name} is missing required fields")
    return value


def _text(
    value: Any, name: str, *, limit: int = 1024, allow_empty: bool = False
) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        _fail("invalid_field", f"{name} must be a bounded string")
    if not allow_empty and not value:
        _fail("invalid_field", f"{name} must not be empty")
    if "\x00" in value:
        _fail("invalid_field", f"{name} contains a forbidden NUL")
    return value


def _identifier(value: Any, name: str) -> str:
    text = _text(value, name, limit=256)
    if not ID_RE.fullmatch(text) or ".." in text or "//" in text:
        _fail("invalid_identifier", f"{name} is not a valid logical identifier")
    return text


def _hash(value: Any, name: str) -> str:
    text = _text(value, name, limit=64)
    if not HASH_RE.fullmatch(text):
        _fail("invalid_hash", f"{name} must be a lowercase SHA-256 hex digest")
    return text


def _nullable_hash(value: Any, name: str) -> str | None:
    return None if value is None else _hash(value, name)


def _nullable_id(value: Any, name: str) -> str | None:
    return None if value is None else _identifier(value, name)


def _timestamp(value: Any, name: str) -> dt.datetime:
    text = _text(value, name, limit=64)
    if not text.endswith("Z"):
        _fail("invalid_timestamp", f"{name} must be an RFC3339 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _fail("invalid_timestamp", f"{name} must be an RFC3339 UTC timestamp")
    if parsed.tzinfo != dt.timezone.utc:
        _fail("invalid_timestamp", f"{name} must be UTC")
    return parsed


def _format_timestamp(value: dt.datetime) -> str:
    value = value.astimezone(dt.timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _trusted_utc(clock: Callable[[], dt.datetime], purpose: str) -> dt.datetime:
    instant = clock()
    if not isinstance(instant, dt.datetime) or instant.tzinfo is None:
        _fail("invalid_clock", f"{purpose} clock must return an aware timestamp")
    return instant.astimezone(dt.timezone.utc)


def _content_id(prefix: str, value: Any) -> tuple[str, str]:
    digest = _sha(canonical_bytes(value))
    return f"{prefix}:{_b64u(bytes.fromhex(digest))}", digest


def _ensure_directory(path: Path, *, create: bool = False, mode: int = 0o700) -> None:
    if create:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
    try:
        st = path.lstat()
    except OSError:
        _fail("unsafe_storage", "required storage directory is unavailable")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        _fail("unsafe_storage", "storage directory must be a real directory")


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        _fail("unsafe_path", "configured target path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        _fail("unsafe_path", "configured target path is invalid")
    return path.as_posix()


def _resolved_file(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    relative = _validate_relative_path(relative)
    current = root
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        if not current.exists():
            if must_exist:
                _fail("artifact_missing", "configured artifact is unavailable")
            continue
        try:
            st = current.lstat()
        except OSError:
            _fail("unsafe_path", "configured artifact has an unsafe ancestor")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            _fail("unsafe_path", "configured artifact has an unsafe ancestor")
    target = root.joinpath(*parts)
    try:
        root_real = root.resolve(strict=True)
        parent_real = target.parent.resolve(strict=must_exist)
    except OSError:
        if must_exist:
            _fail("artifact_missing", "configured artifact is unavailable")
        parent_real = target.parent.resolve(strict=False)
        root_real = root.resolve(strict=True)
    if parent_real != root_real and root_real not in parent_real.parents:
        _fail("unsafe_path", "configured artifact escapes the corpus root")
    if must_exist:
        try:
            st = target.lstat()
        except OSError:
            _fail("artifact_missing", "configured artifact is unavailable")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            _fail(
                "unsafe_path", "configured artifact must be a regular non-symlink file"
            )
    elif target.exists() and target.is_symlink():
        _fail("unsafe_path", "configured target must not be a symlink")
    return target


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    unavailable_code: str,
    unsafe_code: str,
    too_large_code: str,
    changed_code: str,
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        try:
            if path.is_symlink():
                _fail(unsafe_code, f"{label} must be a regular non-symlink file")
        except OSError:
            pass
        _fail(unavailable_code, f"{label} is unavailable")
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            _fail(unsafe_code, f"{label} must be a regular non-symlink file")
        if file_stat.st_size > max_bytes:
            _fail(too_large_code, f"{label} exceeds its byte bound")
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(max_bytes + 1)
        except OSError:
            _fail(unavailable_code, f"{label} could not be read")
        if len(data) != file_stat.st_size:
            _fail(changed_code, f"{label} changed while being read")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    _atomic_bytes(path, canonical_bytes(value) + b"\n", mode=mode)


def _read_json(path: Path, *, max_bytes: int = MAX_EXPORT_BYTES) -> Any:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("state_unavailable", "exchange state is unavailable")
    try:
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            _fail("state_corrupt", "exchange state is not a bounded regular file")
        try:
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(max_bytes + 1)
        except OSError:
            _fail("state_unavailable", "exchange state could not be read")
        return load_json_bytes(data, max_bytes=max_bytes)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_plain_tree(path: Path, *, label: str) -> None:
    """Reject links and special files anywhere in a durable projection tree."""

    try:
        root_stat = path.lstat()
    except OSError:
        _fail("unsafe_projection", f"{label} is unavailable")
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        _fail("unsafe_projection", f"{label} must be a real directory")
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in (*directories, *files):
            entry = Path(parent) / name
            try:
                entry_stat = entry.lstat()
            except OSError:
                _fail("unsafe_projection", f"{label} contains an unavailable entry")
            if stat.S_ISLNK(entry_stat.st_mode) or not (
                stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode)
            ):
                _fail(
                    "unsafe_projection",
                    f"{label} contains a link or special file",
                )


def _atomic_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = link.parent / f".{link.name}.{os.getpid()}.tmp"
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(str(target), tmp)
    os.replace(tmp, link)
    _fsync_directory(link.parent)


def publication_fence_path(data_root: str | os.PathLike[str]) -> Path:
    return Path(data_root) / "exchange" / "v1" / "publisher" / "publication.fence.json"


def assert_publication_stable(data_root: str | os.PathLike[str]) -> None:
    """Fail closed while a publication transaction is between generations."""

    fence = publication_fence_path(data_root)
    if fence.exists() or fence.is_symlink():
        _fail("publication_in_progress", "publication generation is not yet stable")


@contextlib.contextmanager
def _writer_lock(
    data_root: Path, *, exclusive: bool, blocking: bool = False
) -> Iterator[None]:
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = data_root / "lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError:
        _fail("unsafe_lock", "writer lock is unavailable or unsafe")
    lock_stat = os.fstat(descriptor)
    # The librarian lock may deliberately be group-writable across service
    # identities. Its bytes are not authority; only require a real file and
    # refuse symlink following.
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(descriptor)
        _fail("unsafe_lock", "writer lock must be a regular file")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            _fail("writer_busy", "the collective-memory writer boundary is busy")
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class ExchangeCapability:
    capability_id: str
    role: str
    scopes: tuple[str, ...]
    secret: bytes

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ExchangeCapability:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            try:
                if stat.S_ISLNK(os.lstat(path).st_mode):
                    _fail(
                        "unsafe_capability",
                        "capability must be a regular non-symlink file",
                    )
            except OSError:
                pass
            _fail("capability_unavailable", "configured capability is unavailable")
        try:
            st = os.fstat(descriptor)
            if not stat.S_ISREG(st.st_mode):
                _fail(
                    "unsafe_capability",
                    "capability must be a regular non-symlink file",
                )
            if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
                _fail("unsafe_capability", "capability ownership or mode is unsafe")
            if st.st_size > 16 * 1024:
                _fail("unsafe_capability", "capability file exceeds its size bound")
            try:
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    capability_bytes = handle.read(16 * 1024 + 1)
            except OSError:
                _fail(
                    "capability_unavailable", "configured capability could not be read"
                )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        obj = _closed(
            load_json_bytes(capability_bytes, max_bytes=16 * 1024),
            required=("schema", "capability_id", "role", "scopes", "secret"),
            name="capability",
        )
        if obj["schema"] != CAPABILITY_SCHEMA:
            _fail("unsupported_schema", "unsupported capability schema")
        capability_id = _identifier(obj["capability_id"], "capability_id")
        role = _text(obj["role"], "role", limit=64)
        if role not in ("export-reader", "reviewed-publisher"):
            _fail("invalid_capability", "capability role is not supported")
        if not isinstance(obj["scopes"], list) or not obj["scopes"]:
            _fail("invalid_capability", "capability scopes must be a non-empty array")
        scopes = tuple(_identifier(item, "scope") for item in obj["scopes"])
        if len(set(scopes)) != len(scopes):
            _fail("invalid_capability", "capability scopes must be unique")
        secret = _unb64u(_text(obj["secret"], "secret", limit=256))
        if len(secret) < 32:
            _fail("invalid_capability", "capability secret is too short")
        return cls(capability_id, role, scopes, secret)

    def require(self, role: str, scope: str) -> None:
        if self.role != role or ("*" not in self.scopes and scope not in self.scopes):
            _fail("capability_denied", "capability does not authorize this operation")

    def require_role(self, role: str) -> None:
        if self.role != role:
            _fail("capability_denied", "capability does not authorize this operation")


@dataclass(frozen=True)
class TrustKey:
    kid: str
    principal: str
    roles: tuple[str, ...]
    public_key: bytes
    not_before: dt.datetime
    not_after: dt.datetime
    revoked_at: dt.datetime | None


class TrustStore:
    def __init__(self, keys: Mapping[str, TrustKey]):
        self._keys = dict(keys)

    @classmethod
    def from_object(cls, value: Any) -> TrustStore:
        obj = _closed(value, required=("schema", "keys"), name="trust store")
        if obj["schema"] != TRUST_SCHEMA or not isinstance(obj["keys"], list):
            _fail("unsupported_schema", "unsupported trust-store schema")
        keys: dict[str, TrustKey] = {}
        for raw in obj["keys"]:
            item = _closed(
                raw,
                required=(
                    "kid",
                    "principal",
                    "roles",
                    "public_key",
                    "not_before",
                    "not_after",
                    "revoked_at",
                ),
                name="trust key",
            )
            kid = _identifier(item["kid"], "kid")
            if kid in keys:
                _fail("duplicate_key", "trust key IDs must be unique")
            principal = _identifier(item["principal"], "principal")
            if not isinstance(item["roles"], list) or not item["roles"]:
                _fail("invalid_key", "trust-key roles must be a non-empty array")
            roles = tuple(_text(role, "key role", limit=64) for role in item["roles"])
            if not set(roles) <= {"subject-consent", "independent-review"}:
                _fail("invalid_key", "trust key has an unsupported role")
            public_key = _unb64u(_text(item["public_key"], "public_key", limit=128))
            if len(public_key) != 32:
                _fail("invalid_key", "Ed25519 public key must be 32 bytes")
            not_before = _timestamp(item["not_before"], "not_before")
            not_after = _timestamp(item["not_after"], "not_after")
            revoked = (
                None
                if item["revoked_at"] is None
                else _timestamp(item["revoked_at"], "revoked_at")
            )
            if not_after <= not_before:
                _fail("invalid_key", "trust-key validity interval is empty")
            keys[kid] = TrustKey(
                kid, principal, roles, public_key, not_before, not_after, revoked
            )
        return cls(keys)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> TrustStore:
        return cls.from_object(load_json_file(path, max_bytes=1024 * 1024))

    def verify(
        self,
        envelope: Any,
        *,
        role: str,
        expected: Mapping[str, Any],
        at: dt.datetime,
    ) -> tuple[dict[str, Any], str]:
        obj = _closed(
            envelope, required=("schema", "body", "signature"), name="evidence envelope"
        )
        if obj["schema"] != PUBLICATION_EVIDENCE_SCHEMA:
            _fail("unsupported_schema", "unsupported publication-evidence schema")
        body = _closed(
            obj["body"],
            required=(
                "schema",
                "kind",
                "evidence_id",
                "issuer",
                "subject_id",
                "requester_id",
                "action",
                "target_id",
                "source_checkpoint",
                "classification",
                "policy_version",
                "preview_hash",
                "content_hash",
                "issued_at",
                "not_before",
                "not_after",
            ),
            name="evidence body",
        )
        if body["schema"] != PUBLICATION_EVIDENCE_SCHEMA:
            _fail("unsupported_schema", "unsupported evidence body schema")
        expected_kind = "consent" if role == "subject-consent" else "review"
        if body["kind"] != expected_kind:
            _fail(
                "wrong_evidence_role", "evidence kind does not match the required role"
            )
        _identifier(body["evidence_id"], "evidence_id")
        issuer = _identifier(body["issuer"], "issuer")
        for expected_key, expected_value in expected.items():
            if body.get(expected_key) != expected_value:
                _fail(
                    "evidence_mismatch",
                    "signed evidence does not bind the exact publication",
                )
        issued_at = _timestamp(body["issued_at"], "issued_at")
        evidence_not_before = _timestamp(body["not_before"], "not_before")
        evidence_not_after = _timestamp(body["not_after"], "not_after")
        if not (
            evidence_not_before <= issued_at <= evidence_not_after
            and evidence_not_before <= at <= evidence_not_after
        ):
            _fail("stale_evidence", "signed evidence is not currently valid")
        signature = _closed(
            obj["signature"],
            required=("alg", "kid", "value"),
            name="evidence signature",
        )
        if signature["alg"] != "Ed25519":
            _fail(
                "unsupported_signature", "evidence signature algorithm is unsupported"
            )
        kid = _identifier(signature["kid"], "kid")
        trust_key = self._keys.get(kid)
        if (
            trust_key is None
            or trust_key.principal != issuer
            or role not in trust_key.roles
        ):
            _fail(
                "untrusted_evidence",
                "evidence signer is not trusted for the required role",
            )
        if not (
            trust_key.not_before <= issued_at <= trust_key.not_after
            and trust_key.not_before <= at <= trust_key.not_after
        ):
            _fail("stale_key", "evidence signer key is not currently valid")
        if trust_key.revoked_at is not None and (
            issued_at >= trust_key.revoked_at or at >= trust_key.revoked_at
        ):
            _fail("revoked_key", "evidence signer key is revoked")
        raw_signature = _unb64u(_text(signature["value"], "signature", limit=128))
        if len(raw_signature) != 64:
            _fail("invalid_signature", "Ed25519 signature must be 64 bytes")
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )

            Ed25519PublicKey.from_public_bytes(trust_key.public_key).verify(
                raw_signature, canonical_bytes(body)
            )
        except ExchangeError:
            raise
        except (InvalidSignature, ValueError):
            _fail("invalid_signature", "publication evidence signature is invalid")
        return body, _sha(canonical_bytes(obj))


@dataclass(frozen=True)
class ExchangeConfig:
    producer_instance: str
    producer_release: str
    policy_version: str
    targets: Mapping[str, str]
    index_scope: str = "total"

    @classmethod
    def from_object(cls, value: Any) -> ExchangeConfig:
        obj = _closed(
            value,
            required=(
                "schema",
                "producer_instance",
                "producer_release",
                "policy_version",
                "targets",
                "index_scope",
            ),
            name="exchange config",
        )
        if obj["schema"] != CONFIG_SCHEMA:
            _fail("unsupported_schema", "unsupported exchange-config schema")
        producer_instance = _identifier(obj["producer_instance"], "producer_instance")
        producer_release = _identifier(obj["producer_release"], "producer_release")
        policy_version = _identifier(obj["policy_version"], "policy_version")
        if obj["index_scope"] not in ("curated", "total"):
            _fail("invalid_config", "index scope must be curated or total")
        if not isinstance(obj["targets"], list) or not obj["targets"]:
            _fail("invalid_config", "targets must be a non-empty array")
        targets: dict[str, str] = {}
        paths: set[str] = set()
        for raw in obj["targets"]:
            item = _closed(
                raw, required=("target_id", "relative_path"), name="target mapping"
            )
            target_id = _identifier(item["target_id"], "target_id")
            relative = _validate_relative_path(item["relative_path"])
            if target_id in targets or relative in paths:
                _fail("invalid_config", "target IDs and paths must be unique")
            targets[target_id] = relative
            paths.add(relative)
        return cls(
            producer_instance,
            producer_release,
            policy_version,
            targets,
            obj["index_scope"],
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ExchangeConfig:
        return cls.from_object(load_json_file(path, max_bytes=1024 * 1024))


def _source_ref(value: Any) -> dict[str, str]:
    obj = _closed(value, required=("id", "hash"), name="source reference")
    return {
        "id": _identifier(obj["id"], "source reference id"),
        "hash": _hash(obj["hash"], "source reference hash"),
    }


def _checkpoint(value: Any) -> dict[str, str]:
    obj = _closed(value, required=("id", "hash"), name="source checkpoint")
    return {
        "id": _identifier(obj["id"], "checkpoint id"),
        "hash": _hash(obj["hash"], "checkpoint hash"),
    }


def _validate_draft(value: Any, config: ExchangeConfig) -> dict[str, Any]:
    obj = _closed(
        value,
        required=(
            "schema",
            "action",
            "requester_id",
            "subject_id",
            "target_id",
            "source_refs",
            "source_checkpoint",
            "classification",
            "policy_version",
            "media_type",
            "title",
            "body",
            "predecessor_receipt_id",
            "predecessor_receipt_hash",
        ),
        name="publication draft",
    )
    if obj["schema"] != PUBLICATION_DRAFT_SCHEMA:
        _fail("unsupported_schema", "unsupported publication-draft schema")
    action = obj["action"]
    if action not in ("publish", "successor", "tombstone"):
        _fail("invalid_action", "publication action is unsupported")
    requester_id = _identifier(obj["requester_id"], "requester_id")
    subject_id = _identifier(obj["subject_id"], "subject_id")
    target_id = _identifier(obj["target_id"], "target_id")
    if target_id not in config.targets:
        _fail("target_denied", "logical target is not allowlisted")
    if (
        not isinstance(obj["source_refs"], list)
        or not 1 <= len(obj["source_refs"]) <= MAX_SOURCE_REFS
    ):
        _fail("invalid_sources", "source_refs must be a bounded non-empty array")
    source_refs = [_source_ref(item) for item in obj["source_refs"]]
    if source_refs != sorted(source_refs, key=lambda item: (item["id"], item["hash"])):
        _fail("non_canonical_sources", "source_refs must be in canonical order")
    if len({(item["id"], item["hash"]) for item in source_refs}) != len(source_refs):
        _fail("duplicate_source", "source_refs must be unique")
    classification = _identifier(obj["classification"], "classification")
    policy_version = _identifier(obj["policy_version"], "policy_version")
    if policy_version != config.policy_version:
        _fail("policy_mismatch", "publication policy version is not current")
    if obj["media_type"] != "text/markdown; charset=utf-8":
        _fail("unsupported_media_type", "only UTF-8 Markdown publication is supported")
    title = _text(obj["title"], "title", limit=1024, allow_empty=action == "tombstone")
    body = _text(
        obj["body"],
        "body",
        limit=MAX_PUBLICATION_BYTES,
        allow_empty=action == "tombstone",
    )
    predecessor_id = _nullable_id(
        obj["predecessor_receipt_id"], "predecessor_receipt_id"
    )
    predecessor_hash = _nullable_hash(
        obj["predecessor_receipt_hash"], "predecessor_receipt_hash"
    )
    if (predecessor_id is None) != (predecessor_hash is None):
        _fail(
            "invalid_predecessor",
            "predecessor receipt ID and hash must appear together",
        )
    if action == "publish" and predecessor_id is not None:
        _fail("invalid_predecessor", "an initial publication cannot name a predecessor")
    if action != "publish" and predecessor_id is None:
        _fail("missing_predecessor", "a successor or tombstone requires a predecessor")
    if action == "tombstone" and (title or body):
        _fail("invalid_tombstone", "tombstone title and body must be empty")
    return {
        "schema": PUBLICATION_DRAFT_SCHEMA,
        "action": action,
        "requester_id": requester_id,
        "subject_id": subject_id,
        "target_id": target_id,
        "source_refs": source_refs,
        "source_checkpoint": _checkpoint(obj["source_checkpoint"]),
        "classification": classification,
        "policy_version": policy_version,
        "media_type": obj["media_type"],
        "title": title,
        "body": body,
        "predecessor_receipt_id": predecessor_id,
        "predecessor_receipt_hash": predecessor_hash,
    }


def _render(draft: Mapping[str, Any]) -> bytes:
    metadata = {
        "action": draft["action"],
        "classification": draft["classification"],
        "policy_version": draft["policy_version"],
        "schema": "collective-publication-artifact/v1",
        "source_checkpoint": draft["source_checkpoint"],
        "source_refs": draft["source_refs"],
        "subject_id": draft["subject_id"],
        "target_id": draft["target_id"],
    }
    if draft["action"] == "tombstone":
        title = "Publication withdrawn"
        body = "This logical artifact was withdrawn by an explicit reviewed successor."
    else:
        title = draft["title"].replace("\r\n", "\n").replace("\r", "\n").strip()
        body = draft["body"].replace("\r\n", "\n").replace("\r", "\n").rstrip()
    rendered = (
        b"---\n"
        + canonical_bytes(metadata)
        + b"\n---\n# "
        + title.encode("utf-8")
        + b"\n\n"
        + body.encode("utf-8")
        + b"\n"
    )
    if len(rendered) > MAX_PUBLICATION_BYTES:
        _fail(
            "publication_too_large", "final rendered bytes exceed the publication bound"
        )
    for pattern in SECRET_PATTERNS:
        if pattern.search(rendered):
            _fail(
                "secret_detected",
                "final rendered bytes contain forbidden private material",
            )
    return rendered


def _validate_catalog(value: Any) -> dict[str, Any]:
    obj = _closed(
        value,
        required=("schema", "policy_version", "scope_id", "entries"),
        name="export catalog",
    )
    if obj["schema"] != EXPORT_CATALOG_SCHEMA:
        _fail("unsupported_schema", "unsupported export-catalog schema")
    policy_version = _identifier(obj["policy_version"], "policy_version")
    scope_id = _identifier(obj["scope_id"], "scope_id")
    if not isinstance(obj["entries"], list) or len(obj["entries"]) > MAX_ARTIFACTS:
        _fail("catalog_too_large", "export catalog exceeds the artifact bound")
    entries: list[dict[str, Any]] = []
    artifact_ids: set[str] = set()
    logical_ids: set[str] = set()
    for raw in obj["entries"]:
        item = _closed(
            raw,
            required=(
                "artifact_id",
                "logical_id",
                "relative_path",
                "media_type",
                "authors",
                "source_refs",
                "license",
                "consent_scope",
                "classification",
                "predecessor_artifact_id",
                "state",
            ),
            name="export catalog entry",
        )
        artifact_id = _identifier(item["artifact_id"], "artifact_id")
        logical_id = _identifier(item["logical_id"], "logical_id")
        if artifact_id in artifact_ids or logical_id in logical_ids:
            _fail(
                "duplicate_artifact",
                "artifact and logical IDs must be unique in a generation",
            )
        artifact_ids.add(artifact_id)
        logical_ids.add(logical_id)
        state = item["state"]
        if state not in ("active", "tombstone"):
            _fail("invalid_artifact_state", "artifact state is unsupported")
        relative_path = item["relative_path"]
        if state == "active":
            relative_path = _validate_relative_path(relative_path)
        elif relative_path is not None:
            _fail("invalid_tombstone", "an export tombstone cannot name a content path")
        media_type = _text(item["media_type"], "media_type", limit=128)
        if media_type not in (
            "text/markdown; charset=utf-8",
            "text/plain; charset=utf-8",
        ):
            _fail("unsupported_media_type", "export artifact media type is unsupported")
        if not isinstance(item["authors"], list) or not item["authors"]:
            _fail("missing_provenance", "export artifact authors must be non-empty")
        authors = [_identifier(author, "author") for author in item["authors"]]
        if authors != sorted(set(authors)):
            _fail("non_canonical_provenance", "authors must be unique and sorted")
        if (
            not isinstance(item["source_refs"], list)
            or not 1 <= len(item["source_refs"]) <= MAX_SOURCE_REFS
        ):
            _fail(
                "missing_provenance",
                "export source references must be a bounded non-empty array",
            )
        source_refs = [_source_ref(ref) for ref in item["source_refs"]]
        if source_refs != sorted(source_refs, key=lambda ref: (ref["id"], ref["hash"])):
            _fail("non_canonical_sources", "export source references must be sorted")
        if len({(ref["id"], ref["hash"]) for ref in source_refs}) != len(source_refs):
            _fail("duplicate_source", "export source references must be unique")
        predecessor = _nullable_id(
            item["predecessor_artifact_id"], "predecessor_artifact_id"
        )
        entries.append(
            {
                "artifact_id": artifact_id,
                "logical_id": logical_id,
                "relative_path": relative_path,
                "media_type": media_type,
                "authors": authors,
                "source_refs": source_refs,
                "license": _identifier(item["license"], "license"),
                "consent_scope": _identifier(item["consent_scope"], "consent_scope"),
                "classification": _identifier(item["classification"], "classification"),
                "predecessor_artifact_id": predecessor,
                "state": state,
            }
        )
    if entries != sorted(entries, key=lambda entry: entry["artifact_id"]):
        _fail(
            "non_canonical_catalog",
            "export catalog entries must be sorted by artifact_id",
        )
    if any(
        entry["consent_scope"] != scope_id or entry["classification"] != scope_id
        for entry in entries
    ):
        _fail(
            "scope_violation",
            "export artifacts must match the accepted scope and classification",
        )
    return {
        "schema": EXPORT_CATALOG_SCHEMA,
        "policy_version": policy_version,
        "scope_id": scope_id,
        "entries": entries,
    }


class ExportBoundary:
    """Read-only corpus boundary with a distinct immutable export cache."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        data_root: str | os.PathLike[str],
        config: ExchangeConfig,
        capability: ExchangeCapability,
        catalog_provider: Callable[[str], Any],
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ):
        self.root = Path(root).resolve(strict=True)
        self.data_root = Path(data_root).resolve(strict=False)
        self.config = config
        self.capability = capability
        self.catalog_provider = catalog_provider
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        _ensure_directory(self.root)
        _ensure_directory(self.data_root, create=True)
        self.state_root = self.data_root / "exchange" / "v1" / "export-reader"
        self.generations = self.state_root / "generations"
        self.pending = self.state_root / "pending"
        _ensure_directory(self.generations, create=True)
        _ensure_directory(self.pending, create=True)

    def _current_manifest(self) -> dict[str, Any] | None:
        current = self.state_root / "current"
        if not current.is_symlink():
            if current.exists():
                _fail("state_corrupt", "current export pointer is not a symlink")
            return None
        try:
            generation = current.resolve(strict=True)
        except OSError:
            _fail("state_corrupt", "current export generation is unavailable")
        if generation.parent != self.generations:
            _fail("state_corrupt", "current export generation escapes its store")
        manifest = _read_json(generation / "manifest.json")
        return self._validate_stored_manifest(manifest, generation)

    def _generation_directory(self, generation_id: Any) -> Path:
        prefix = "cm:export:v1:"
        if (
            not isinstance(generation_id, str)
            or not generation_id.startswith(prefix)
            or not B64U_SHA256_RE.fullmatch(generation_id.removeprefix(prefix))
        ):
            _fail("invalid_generation", "export generation ID is invalid")
        directory = self.generations / generation_id.replace(":", "_")
        try:
            st = directory.lstat()
        except OSError:
            _fail("unknown_generation", "export generation is unavailable")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            _fail("state_corrupt", "export generation must be a real directory")
        try:
            if directory.resolve(strict=True).parent != self.generations.resolve(
                strict=True
            ):
                _fail("state_corrupt", "export generation escapes its store")
        except OSError:
            _fail("state_corrupt", "export generation is unavailable")
        return directory

    def _validate_stored_manifest(
        self, manifest: Any, directory: Path
    ) -> dict[str, Any]:
        obj = _closed(
            manifest,
            required=("schema", "generation_id", "manifest_hash", "body"),
            name="stored export manifest",
        )
        if obj["schema"] != EXPORT_MANIFEST_SCHEMA:
            _fail("state_corrupt", "stored export manifest schema is invalid")
        expected_id, expected_hash = _content_id("cm:export:v1", obj["body"])
        if (
            obj["generation_id"] != expected_id
            or obj["manifest_hash"] != expected_hash
            or directory.name != expected_id.replace(":", "_")
        ):
            _fail("state_corrupt", "stored export manifest identity is invalid")
        return obj

    def _stored_generation(self, generation_id: Any) -> tuple[Path, dict[str, Any]]:
        directory = self._generation_directory(generation_id)
        manifest = self._validate_stored_manifest(
            _read_json(directory / "manifest.json"), directory
        )
        return directory, manifest

    def _projection_checkpoint(self) -> dict[str, str | None]:
        index_generation: str | None = None
        ui_generation: str | None = None
        index_db = self.data_root / "index.db"
        if index_db.exists() or index_db.is_symlink():
            try:
                st = index_db.lstat()
            except OSError:
                _fail("projection_unavailable", "index projection is unavailable")
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                _fail("unsafe_projection", "index projection must be a regular file")
            try:
                connection = sqlite3.connect(f"file:{index_db}?mode=ro", uri=True)
                row = connection.execute(
                    "SELECT v FROM meta WHERE k='index_generation'"
                ).fetchone()
                connection.close()
            except sqlite3.Error:
                _fail("projection_unavailable", "index projection cannot be verified")
            if not row:
                _fail("projection_unavailable", "index generation is unavailable")
            if not isinstance(row[0], (str, int)) or isinstance(row[0], bool):
                _fail("projection_unavailable", "index generation is invalid")
            index_generation = _text(str(row[0]), "index_generation", limit=128)

        ui = self.data_root / "ui"
        if ui.exists() or ui.is_symlink():
            try:
                ui_real = ui.resolve(strict=True)
                data_real = self.data_root.resolve(strict=True)
            except OSError:
                _fail("projection_unavailable", "UI projection is unavailable")
            if ui_real == data_real or data_real not in ui_real.parents:
                _fail("unsafe_projection", "UI projection escapes the data root")
            meta_path = ui_real / "meta.json"
            if meta_path.is_file() and not meta_path.is_symlink():
                meta = _read_json(meta_path, max_bytes=1024 * 1024)
                if not isinstance(meta, Mapping):
                    _fail("projection_unavailable", "UI projection metadata is invalid")
                raw_ui_generation = meta.get("generation")
                raw_ui_index = meta.get("index_generation")
            else:
                ui_db = ui_real / "ui_v2.db"
                try:
                    st = ui_db.lstat()
                except OSError:
                    _fail("projection_unavailable", "UI projection is unavailable")
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    _fail("unsafe_projection", "UI projection must be a regular file")
                try:
                    connection = sqlite3.connect(f"file:{ui_db}?mode=ro", uri=True)
                    rows = dict(connection.execute("SELECT k,v FROM meta"))
                    connection.close()
                    raw_ui_generation = json.loads(rows["generation"])
                    raw_ui_index = json.loads(rows["index_generation"])
                except (KeyError, ValueError, TypeError, sqlite3.Error):
                    _fail(
                        "projection_unavailable",
                        "UI projection metadata cannot be verified",
                    )
            if (
                not isinstance(raw_ui_generation, (str, int))
                or isinstance(raw_ui_generation, bool)
                or not isinstance(raw_ui_index, (str, int))
                or isinstance(raw_ui_index, bool)
            ):
                _fail("projection_unavailable", "UI projection metadata is invalid")
            ui_generation = _text(str(raw_ui_generation), "ui_generation", limit=256)
            ui_index_generation = _text(
                str(raw_ui_index), "ui index_generation", limit=128
            )
            if index_generation is None or ui_index_generation != index_generation:
                _fail(
                    "projection_drift",
                    "index and UI projections are not one accepted generation",
                )
        return {
            "index_generation": index_generation,
            "ui_generation": ui_generation,
        }

    def create(self, scope_id: str) -> dict[str, Any]:
        scope_id = _identifier(scope_id, "scope_id")
        self.capability.require("export-reader", scope_id)
        try:
            catalog = self.catalog_provider(scope_id)
        except ExchangeError:
            raise
        except Exception:  # noqa: BLE001 - provider failures are closed at the boundary
            _fail("catalog_unavailable", "accepted export catalog is unavailable")
        normalized = _validate_catalog(catalog)
        if normalized["scope_id"] != scope_id:
            _fail("scope_mismatch", "accepted catalog does not match requested scope")
        if normalized["policy_version"] != self.config.policy_version:
            _fail("policy_mismatch", "export policy version is not current")
        created_at = _format_timestamp(_trusted_utc(self.clock, "export"))
        # Export generations are read-only with respect to the corpus, but the
        # export cache itself has one writer.  Keep that serialization separate
        # from the shared corpus lock so concurrent exporters cannot race the
        # pending/current generation state.
        with (
            _writer_lock(self.state_root, exclusive=True),
            _writer_lock(self.data_root, exclusive=False),
        ):
            assert_publication_stable(self.data_root)
            return self._create_locked(normalized, created_at=created_at)

    def manifest(self, generation_id: str | None = None) -> dict[str, Any]:
        """Return a current or historical immutable export manifest.

        Historical lookup lets a consumer that missed multiple generations
        walk ``predecessor_generation`` backwards and verify the entire chain
        before importing it oldest-first. No cache or corpus mutation occurs.
        """

        self.capability.require_role("export-reader")
        if generation_id is None:
            manifest = self._current_manifest()
            if manifest is None:
                _fail("unknown_generation", "no export generation is available")
        else:
            _directory, manifest = self._stored_generation(generation_id)
        self.capability.require("export-reader", manifest["body"]["scope_id"])
        return manifest

    def _create_locked(
        self, normalized: Mapping[str, Any], *, created_at: str
    ) -> dict[str, Any]:
        current = self._current_manifest()
        projection = self._projection_checkpoint()
        previous_by_logical: dict[str, dict[str, Any]] = {}
        if current:
            previous_by_logical = {
                entry["logical_id"]: entry for entry in current["body"]["artifacts"]
            }
        described: list[dict[str, Any]] = []
        objects: dict[str, bytes] = {}
        total = 0
        for entry in normalized["entries"]:
            previous = previous_by_logical.get(entry["logical_id"])
            predecessor = entry["predecessor_artifact_id"]
            if previous is None:
                if predecessor is not None:
                    _fail(
                        "dangling_predecessor",
                        "initial export artifact names an unknown predecessor",
                    )
            elif entry["artifact_id"] == previous["artifact_id"]:
                if predecessor != previous["predecessor_artifact_id"]:
                    _fail(
                        "artifact_collision",
                        "stable artifact ID changed its predecessor",
                    )
            elif predecessor != previous["artifact_id"]:
                _fail(
                    "generation_fork",
                    "changed export artifact does not extend the accepted predecessor",
                )
            if entry["state"] == "active":
                path = _resolved_file(self.root, entry["relative_path"])
                content = _read_regular_bytes(
                    path,
                    max_bytes=MAX_ARTIFACT_BYTES,
                    label="export artifact",
                    unavailable_code="artifact_unavailable",
                    unsafe_code="unsafe_path",
                    too_large_code="artifact_too_large",
                    changed_code="artifact_changed",
                )
                size = len(content)
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    _fail("invalid_utf8", "text export artifact is not UTF-8")
                content_hash = _sha(content)
                content_ref = f"sha256:{content_hash}"
                objects.setdefault(content_hash, content)
                total += len(content)
            else:
                content_hash = None
                content_ref = None
                size = 0
            if total > MAX_EXPORT_BYTES:
                _fail(
                    "export_too_large",
                    "export generation exceeds the total byte bound",
                )
            descriptor = {
                key: value for key, value in entry.items() if key != "relative_path"
            } | {
                "content_hash": content_hash,
                "content_length": size,
                "content_ref": content_ref,
            }
            if (
                previous is not None
                and entry["artifact_id"] == previous["artifact_id"]
                and descriptor != previous
            ):
                _fail(
                    "artifact_collision",
                    "stable artifact ID changed content or provenance",
                )
            described.append(descriptor)
        current_logical = {entry["logical_id"] for entry in described}
        removed = sorted(set(previous_by_logical) - current_logical)
        if removed:
            _fail(
                "implicit_removal",
                "removed artifacts require explicit tombstone successors",
            )
        state_projection = {
            "policy_version": normalized["policy_version"],
            "scope_id": normalized["scope_id"],
            "projection": projection,
            "artifacts": described,
        }
        state_digest = _sha(canonical_bytes(state_projection))
        if current and current["body"].get("state_digest") == state_digest:
            return current
        pending_path = self.pending / f"{state_digest}.json"
        if pending_path.exists():
            pending = _read_json(pending_path, max_bytes=16 * 1024)
            created_at = pending.get("created_at")
            _timestamp(created_at, "created_at")
        else:
            _atomic_json(
                pending_path,
                {"state_digest": state_digest, "created_at": created_at},
            )
        predecessor_generation = current["generation_id"] if current else None
        body = {
            "producer_instance": self.config.producer_instance,
            "producer_release": self.config.producer_release,
            "policy_version": normalized["policy_version"],
            "scope_id": normalized["scope_id"],
            "projection": projection,
            "created_at": created_at,
            "predecessor_generation": predecessor_generation,
            "state_digest": state_digest,
            "artifact_count": len(described),
            "total_content_bytes": total,
            "artifacts": described,
        }
        generation_id, manifest_hash = _content_id("cm:export:v1", body)
        manifest = {
            "schema": EXPORT_MANIFEST_SCHEMA,
            "generation_id": generation_id,
            "manifest_hash": manifest_hash,
            "body": body,
        }
        final = self.generations / generation_id.replace(":", "_")
        if final.exists():
            existing = _read_json(final / "manifest.json")
            if existing != manifest:
                _fail("generation_collision", "export generation ID collision")
        else:
            stage = Path(tempfile.mkdtemp(prefix=".export.", dir=self.generations))
            try:
                object_root = stage / "objects"
                object_root.mkdir(mode=0o700)
                for digest, content in sorted(objects.items()):
                    _atomic_bytes(object_root / digest, content, mode=0o600)
                _atomic_json(stage / "manifest.json", manifest)
                _fsync_directory(object_root)
                _fsync_directory(stage)
                os.rename(stage, final)
                _fsync_directory(self.generations)
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
        _atomic_symlink(self.state_root / "current", final)
        try:
            pending_path.unlink()
            _fsync_directory(self.pending)
        except FileNotFoundError:
            pass
        return manifest

    def page(
        self, generation_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        self.capability.require_role("export-reader")
        if not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            _fail("invalid_page", "page limit is outside the contract bound")
        _directory, manifest = self._stored_generation(generation_id)
        scope = manifest["body"]["scope_id"]
        self.capability.require("export-reader", scope)
        offset = 0
        if cursor is not None:
            try:
                cursor_obj = load_json_bytes(_unb64u(cursor), max_bytes=4096)
            except ExchangeError:
                _fail("invalid_cursor", "export cursor is invalid")
            cursor_obj = _closed(
                cursor_obj,
                required=("generation_id", "manifest_hash", "offset", "checksum"),
                name="cursor",
            )
            unsigned = {
                key: cursor_obj[key]
                for key in ("generation_id", "manifest_hash", "offset")
            }
            if cursor_obj["checksum"] != _sha(canonical_bytes(unsigned)):
                _fail("invalid_cursor", "export cursor checksum is invalid")
            if (
                cursor_obj["generation_id"] != generation_id
                or cursor_obj["manifest_hash"] != manifest["manifest_hash"]
            ):
                _fail(
                    "mixed_generation",
                    "export cursor belongs to a different generation",
                )
            offset = cursor_obj["offset"]
            if not isinstance(offset, int) or offset < 0:
                _fail("invalid_cursor", "export cursor offset is invalid")
        artifacts = manifest["body"]["artifacts"]
        selected = artifacts[offset : offset + limit]
        next_offset = offset + len(selected)
        next_cursor = None
        if next_offset < len(artifacts):
            unsigned = {
                "generation_id": generation_id,
                "manifest_hash": manifest["manifest_hash"],
                "offset": next_offset,
            }
            next_cursor = _b64u(
                canonical_bytes(
                    unsigned | {"checksum": _sha(canonical_bytes(unsigned))}
                )
            )
        return {
            "schema": EXPORT_PAGE_SCHEMA,
            "generation_id": generation_id,
            "manifest_hash": manifest["manifest_hash"],
            "offset": offset,
            "limit": limit,
            "artifacts": selected,
            "next_cursor": next_cursor,
        }

    def object_bytes(self, generation_id: str, content_ref: str) -> bytes:
        self.capability.require_role("export-reader")
        if not isinstance(content_ref, str) or not content_ref.startswith("sha256:"):
            _fail("invalid_content_ref", "content reference is invalid")
        digest = _hash(content_ref.removeprefix("sha256:"), "content_ref")
        directory, manifest = self._stored_generation(generation_id)
        self.capability.require("export-reader", manifest["body"]["scope_id"])
        declared = {
            entry["content_hash"]
            for entry in manifest["body"]["artifacts"]
            if entry["content_hash"]
        }
        if digest not in declared:
            _fail(
                "content_not_declared",
                "content reference is not declared by this generation",
            )
        path = directory / "objects" / digest
        data = _read_regular_bytes(
            path,
            max_bytes=MAX_ARTIFACT_BYTES,
            label="declared export content",
            unavailable_code="content_unavailable",
            unsafe_code="content_corrupt",
            too_large_code="content_corrupt",
            changed_code="content_corrupt",
        )
        if _sha(data) != digest:
            _fail("content_corrupt", "declared export content hash does not match")
        return data


class DefaultProjectionRunner:
    """Build and verify the repository's real index and Atlas projections."""

    def __init__(
        self,
        root: Path,
        data_root: Path,
        index_scope: str,
        fault_hook: Callable[[str], None],
    ):
        self.root = root
        self.data_root = data_root
        self.index_scope = index_scope
        self.fault_hook = fault_hook
        self.code_home = Path(__file__).resolve().parent

    def snapshot(self, destination: Path) -> dict[str, Any]:
        backup = destination / "projection-backup"
        backup.mkdir(mode=0o700)
        present: list[str] = []
        for name in (
            "index.db",
            "manifest.json",
            "corpus_audit.json",
            "corpus_audit.md",
            "ui_status.json",
        ):
            source = self.data_root / name
            if source.is_symlink() or (source.exists() and not source.is_file()):
                _fail("unsafe_projection", "projection snapshot source is unsafe")
            if source.is_file():
                shutil.copy2(source, backup / name)
                present.append(name)
        ui = self.data_root / "ui"
        if ui.exists() and not (ui.is_symlink() or ui.is_dir()):
            _fail("unsafe_projection", "UI projection source is unsafe")
        if ui.is_symlink() or ui.is_dir():
            ui_real = ui.resolve(strict=True)
            if ui_real != self.data_root and self.data_root not in ui_real.parents:
                _fail("unsafe_projection", "UI projection escapes the data root")
            _require_plain_tree(ui_real, label="UI projection snapshot")
            # Preserve links rather than following them if a writer ignoring the
            # shared lock races this copy; post-copy validation then rejects the
            # snapshot without intentionally dereferencing a link target.
            shutil.copytree(
                ui_real,
                backup / "ui",
                copy_function=shutil.copy2,
                symlinks=True,
            )
            _require_plain_tree(backup / "ui", label="UI projection snapshot")
            present.append("ui")
        _atomic_json(backup / "snapshot.json", {"present": sorted(present)})
        return {"present": sorted(present)}

    def restore(self, destination: Path) -> None:
        backup = destination / "projection-backup"
        snapshot = _read_json(backup / "snapshot.json", max_bytes=16 * 1024)
        present = set(snapshot.get("present", []))
        for name in (
            "index.db",
            "manifest.json",
            "corpus_audit.json",
            "corpus_audit.md",
            "ui_status.json",
        ):
            target = self.data_root / name
            if name in present:
                _atomic_bytes(target, (backup / name).read_bytes(), mode=0o644)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
        ui_link = self.data_root / "ui"
        if "ui" in present:
            _require_plain_tree(backup / "ui", label="UI projection rollback")
            restore_root = self.data_root / f"ui.exchange-restore.{destination.name}"
            if restore_root.exists():
                shutil.rmtree(restore_root)
            shutil.copytree(backup / "ui", restore_root, symlinks=True)
            _require_plain_tree(restore_root, label="UI projection rollback")
            _atomic_symlink(ui_link, restore_root)
        else:
            try:
                ui_link.unlink()
            except FileNotFoundError:
                if ui_link.is_dir():
                    shutil.rmtree(ui_link)

    def _run(self, argv: Sequence[str]) -> None:
        env = dict(os.environ)
        env.update(
            {
                "MAPA_ROOT": str(self.root),
                "MAPA_DATA": str(self.data_root),
                "MAPA_ALLOW_MODEL_DOWNLOAD": "0",
                "PYTHONPATH": str(self.code_home),
            }
        )
        result = subprocess.run(
            list(argv),
            cwd=self.code_home.parent,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            _fail("projection_failed", "collective-memory projection rebuild failed")

    def build(self) -> None:
        self._run(
            (
                sys.executable,
                str(self.code_home / "tier1.py"),
                "index",
                "--scope",
                self.index_scope,
            )
        )
        self.fault_hook("index-published")
        self._run(
            (
                sys.executable,
                str(self.code_home / "ui_builder.py"),
                "build",
                "--mode",
                "fast",
            )
        )
        self.fault_hook("ui-published")

    def verify(self, relative_path: str, content_hash: str) -> dict[str, Any]:
        db = self.data_root / "index.db"
        ui = self.data_root / "ui"
        try:
            db_stat = db.lstat()
            ui_root = ui.resolve(strict=True)
            data_root = self.data_root.resolve(strict=True)
            ui_db = ui_root / "ui_v2.db"
            ui_db_stat = ui_db.lstat()
        except OSError:
            _fail(
                "effect_unverifiable",
                "required publication projections are unavailable",
            )
        if (
            stat.S_ISLNK(db_stat.st_mode)
            or not stat.S_ISREG(db_stat.st_mode)
            or ui_root == data_root
            or data_root not in ui_root.parents
            or stat.S_ISLNK(ui_db_stat.st_mode)
            or not stat.S_ISREG(ui_db_stat.st_mode)
        ):
            _fail("unsafe_projection", "publication projection path is unsafe")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute(
                "SELECT content_hash FROM docs WHERE doc_id=?", (relative_path,)
            ).fetchone()
            meta = dict(con.execute("SELECT k,v FROM meta").fetchall())
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            ucon = sqlite3.connect(f"file:{ui_db}?mode=ro", uri=True)
            ui_row = ucon.execute(
                "SELECT 1 FROM docs WHERE doc_id=?", (relative_path,)
            ).fetchone()
            ui_meta = {
                key: json.loads(value)
                for key, value in ucon.execute("SELECT k,v FROM meta")
            }
            ui_integrity = ucon.execute("PRAGMA integrity_check").fetchone()[0]
            ucon.close()
        except ExchangeError:
            raise
        except (OSError, sqlite3.Error, json.JSONDecodeError, KeyError, ValueError):
            _fail("effect_unverifiable", "publication projections cannot be verified")
        if (
            not row
            or row[0] != content_hash
            or not ui_row
            or integrity != "ok"
            or ui_integrity != "ok"
        ):
            _fail(
                "effect_truth_discrepancy",
                "observed publication projection contradicts the receipt",
            )
        if str(ui_meta.get("index_generation")) != str(meta.get("index_generation")):
            _fail(
                "effect_truth_discrepancy", "index and Atlas generations do not agree"
            )
        return {
            "index_generation": str(meta.get("index_generation")),
            "ui_generation": str(ui_meta.get("generation")),
            "index_content_hash": content_hash,
        }


class PublicationBoundary:
    """Reviewed, crash-recoverable publication transaction."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        data_root: str | os.PathLike[str],
        config: ExchangeConfig,
        capability: ExchangeCapability,
        trust_store: TrustStore,
        *,
        fault_hook: Callable[[str], None] | None = None,
        projection_runner: Any | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ):
        self.root = Path(root).resolve(strict=True)
        self.data_root = Path(data_root).resolve(strict=False)
        self.config = config
        self.capability = capability
        self.trust_store = trust_store
        self.fault_hook = fault_hook or (lambda _stage: None)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        _ensure_directory(self.root)
        _ensure_directory(self.data_root, create=True)
        self.state_root = self.data_root / "exchange" / "v1" / "publisher"
        self.generations = self.state_root / "generations"
        self.transactions = self.state_root / "transactions"
        _ensure_directory(self.generations, create=True)
        _ensure_directory(self.transactions, create=True)
        self.fence = self.state_root / "publication.fence.json"
        self.runner = projection_runner or DefaultProjectionRunner(
            self.root, self.data_root, config.index_scope, self.fault_hook
        )

    def _trusted_now(self) -> dt.datetime:
        return _trusted_utc(self.clock, "publication")

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "schema": PUBLICATION_STATE_SCHEMA,
            "generation": 0,
            "previous_state_hash": None,
            "targets": {},
            "idempotency": {},
            "receipts": {},
        }

    def _current_state(self) -> tuple[dict[str, Any], str | None]:
        current = self.state_root / "current"
        if not current.is_symlink():
            if current.exists():
                _fail("state_corrupt", "current publication pointer is not a symlink")
            state = self._initial_state()
            return state, _sha(canonical_bytes(state))
        try:
            generation = current.resolve(strict=True)
        except OSError:
            _fail("state_corrupt", "current publication generation is unavailable")
        if generation.parent != self.generations:
            _fail("state_corrupt", "current publication generation escapes its store")
        state = _read_json(generation / "state.json")
        if state.get("schema") != PUBLICATION_STATE_SCHEMA:
            _fail("state_corrupt", "publication state schema is invalid")
        digest = _sha(canonical_bytes(state))
        if generation.name != digest:
            _fail(
                "state_corrupt", "publication state hash does not match its generation"
            )
        return state, digest

    def _effect_truth(
        self, target_id: str, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        relative = self.config.targets[target_id]
        target = _resolved_file(self.root, relative, must_exist=False)
        if not target.is_file() or target.is_symlink():
            _fail(
                "effect_truth_discrepancy", "recorded publication target is not present"
            )
        content = _read_regular_bytes(
            target,
            max_bytes=MAX_PUBLICATION_BYTES,
            label="publication target",
            unavailable_code="effect_unverifiable",
            unsafe_code="effect_truth_discrepancy",
            too_large_code="effect_truth_discrepancy",
            changed_code="effect_unverifiable",
        )
        if (
            _sha(content) != record["content_hash"]
            or len(content) != record["content_length"]
        ):
            _fail(
                "effect_truth_discrepancy",
                "recorded publication target bytes contradict current state",
            )
        return self.runner.verify(relative, record["content_hash"])

    def _before(
        self, state: Mapping[str, Any], target_id: str
    ) -> dict[str, Any] | None:
        value = state["targets"].get(target_id)
        if value is None:
            target = _resolved_file(
                self.root, self.config.targets[target_id], must_exist=False
            )
            if target.exists() or target.is_symlink():
                _fail(
                    "target_drift",
                    "allowlisted target exists without an accepted receipt",
                )
            return None
        self._effect_truth(target_id, value)
        return dict(value)

    def preview(self, draft: Any) -> dict[str, Any]:
        normalized = _validate_draft(draft, self.config)
        self.capability.require("reviewed-publisher", normalized["target_id"])
        rendered = _render(normalized)
        with _writer_lock(self.data_root, exclusive=False):
            assert_publication_stable(self.data_root)
            state, state_hash = self._current_state()
            before = self._before(state, normalized["target_id"])
            predecessor_id = normalized["predecessor_receipt_id"]
            predecessor_hash = normalized["predecessor_receipt_hash"]
            if normalized["action"] == "publish":
                if before is not None:
                    _fail(
                        "target_already_tracked",
                        "initial publication target already has a receipt",
                    )
            else:
                if before is None or before["state"] != "active":
                    _fail(
                        "invalid_predecessor",
                        "successor target has no current active predecessor",
                    )
                if (
                    before["receipt_id"] != predecessor_id
                    or before["receipt_hash"] != predecessor_hash
                ):
                    _fail(
                        "stale_predecessor",
                        "successor does not name the current predecessor",
                    )
            draft_hash = _sha(canonical_bytes(normalized))
            body = {
                "draft_hash": draft_hash,
                "state_hash": state_hash,
                "before": before,
                "rendered": {
                    "content_hash": _sha(rendered),
                    "content_length": len(rendered),
                    "media_type": normalized["media_type"],
                    "bytes_b64": _b64u(rendered),
                },
            }
            preview_id, preview_hash = _content_id("cm:publication-preview:v1", body)
            return {
                "schema": PUBLICATION_PREVIEW_SCHEMA,
                "preview_id": preview_id,
                "preview_hash": preview_hash,
                "body": body,
            }

    def _normalize_request_shape(self, request: Any) -> dict[str, Any]:
        obj = _closed(
            request,
            required=(
                "schema",
                "draft",
                "preview_hash",
                "idempotency_key",
                "consent",
                "review",
            ),
            name="publication request",
        )
        if obj["schema"] != PUBLICATION_REQUEST_SCHEMA:
            _fail("unsupported_schema", "unsupported publication-request schema")
        draft = _validate_draft(obj["draft"], self.config)
        preview_hash = _hash(obj["preview_hash"], "preview_hash")
        idempotency_key = _identifier(obj["idempotency_key"], "idempotency_key")
        # Evidence envelopes remain part of the exact canonical request.  Their
        # closed shapes/signatures are checked below for a fresh effect; an
        # already accepted identical request may be replayed from its verified
        # receipt without depending on now-expired evidence.
        _closed(
            obj["consent"],
            required=("schema", "body", "signature"),
            name="consent evidence",
        )
        _closed(
            obj["review"],
            required=("schema", "body", "signature"),
            name="review evidence",
        )
        return {
            "schema": PUBLICATION_REQUEST_SCHEMA,
            "draft": draft,
            "preview_hash": preview_hash,
            "idempotency_key": idempotency_key,
            "consent": obj["consent"],
            "review": obj["review"],
        }

    def _validate_request(
        self, request: Any, at: dt.datetime
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        normalized = self._normalize_request_shape(request)
        draft = normalized["draft"]
        preview_hash = normalized["preview_hash"]
        preview = self.preview(draft)
        if preview["preview_hash"] != preview_hash:
            _fail(
                "stale_preview", "publication preview no longer matches current state"
            )
        expected = {
            "subject_id": draft["subject_id"],
            "requester_id": draft["requester_id"],
            "action": draft["action"],
            "target_id": draft["target_id"],
            "source_checkpoint": draft["source_checkpoint"],
            "classification": draft["classification"],
            "policy_version": draft["policy_version"],
            "preview_hash": preview_hash,
            "content_hash": preview["body"]["rendered"]["content_hash"],
        }
        consent, consent_hash = self.trust_store.verify(
            normalized["consent"], role="subject-consent", expected=expected, at=at
        )
        review, review_hash = self.trust_store.verify(
            normalized["review"], role="independent-review", expected=expected, at=at
        )
        if consent["issuer"] != draft["subject_id"]:
            _fail("invalid_consent", "consent signer is not the publication subject")
        if review["issuer"] in {
            draft["subject_id"],
            draft["requester_id"],
            consent["issuer"],
        }:
            _fail("self_review", "publication review is not independent")
        return normalized, preview, consent_hash, review_hash

    def plan(self, request: Any) -> dict[str, Any]:
        instant = self._trusted_now()
        normalized, preview, consent_hash, review_hash = self._validate_request(
            request, instant
        )
        draft = normalized["draft"]
        self.capability.require("reviewed-publisher", draft["target_id"])
        return self._plan_from_validated(normalized, preview, consent_hash, review_hash)

    def _plan_from_validated(
        self,
        normalized: Mapping[str, Any],
        preview: Mapping[str, Any],
        consent_hash: str,
        review_hash: str,
    ) -> dict[str, Any]:
        draft = normalized["draft"]
        request_hash = _sha(canonical_bytes(normalized))
        body = {
            "request_hash": request_hash,
            "preview_hash": preview["preview_hash"],
            "target_id": draft["target_id"],
            "action": draft["action"],
            "before": preview["body"]["before"],
            "after": {
                "content_hash": preview["body"]["rendered"]["content_hash"],
                "content_length": preview["body"]["rendered"]["content_length"],
                "media_type": draft["media_type"],
                "state": "tombstone" if draft["action"] == "tombstone" else "active",
            },
            "consent_hash": consent_hash,
            "review_hash": review_hash,
        }
        plan_id, plan_hash = _content_id("cm:publication-plan:v1", body)
        return {
            "schema": PUBLICATION_PLAN_SCHEMA,
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "body": body,
        }

    def _validate_plan(self, plan: Any, expected: Mapping[str, Any]) -> dict[str, Any]:
        obj = _closed(
            plan,
            required=("schema", "plan_id", "plan_hash", "body"),
            name="publication plan",
        )
        if obj["schema"] != PUBLICATION_PLAN_SCHEMA or obj != expected:
            _fail(
                "plan_mismatch",
                "publication plan is not the exact current deterministic plan",
            )
        return obj

    def _write_journal(self, tx: Path, journal: Mapping[str, Any]) -> None:
        _atomic_json(tx / "journal.json", dict(journal))

    def _transaction_directories(self) -> list[Path]:
        _ensure_directory(self.transactions)
        directories: list[Path] = []
        for path in sorted(self.transactions.iterdir()):
            try:
                entry_stat = path.lstat()
            except OSError:
                _fail("state_corrupt", "publication transaction entry is unavailable")
            if stat.S_ISLNK(entry_stat.st_mode):
                _fail("state_corrupt", "publication transaction must not be a symlink")
            if not stat.S_ISDIR(entry_stat.st_mode):
                _fail("state_corrupt", "publication transaction entry is invalid")
            directories.append(path)
        return directories

    def _install_target(self, target: Path, content: bytes) -> None:
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _resolved_file(
            self.root,
            str(target.relative_to(self.root)).replace(os.sep, "/"),
            must_exist=False,
        )
        _atomic_bytes(target, content, mode=0o644)

    def _rollback(self, tx: Path, journal: dict[str, Any]) -> None:
        relative = journal["relative_path"]
        target = _resolved_file(self.root, relative, must_exist=False)
        old = tx / "target.before"
        if journal["target_existed"]:
            self._install_target(target, old.read_bytes())
        else:
            try:
                target.unlink()
                _fsync_directory(target.parent)
            except FileNotFoundError:
                pass
            for relative_parent in journal.get("created_parent_paths", []):
                parent = _resolved_file(self.root, relative_parent, must_exist=False)
                try:
                    st = parent.lstat()
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                        _fail(
                            "unsafe_path",
                            "publication-created target parent is unsafe",
                        )
                    parent.rmdir()
                    _fsync_directory(parent.parent)
                except FileNotFoundError:
                    continue
                except OSError:
                    _fail(
                        "rollback_failed",
                        "publication-created target parent could not be restored",
                    )
        self.runner.restore(tx)
        journal["stage"] = "rolled-back"
        self._write_journal(tx, journal)
        try:
            self.fence.unlink()
            _fsync_directory(self.fence.parent)
        except FileNotFoundError:
            pass

    def recover(self) -> list[dict[str, Any]]:
        self.capability.require_role("reviewed-publisher")
        recovered: list[dict[str, Any]] = []
        with _writer_lock(self.data_root, exclusive=True):
            _state, current_hash = self._current_state()
            for tx in self._transaction_directories():
                journal_path = tx / "journal.json"
                if not journal_path.exists():
                    # No supported effect happens before both snapshot and
                    # journal are durable. An orphan without a matching fence
                    # is therefore a safely abandoned pre-prepare attempt.
                    fenced = False
                    if self.fence.exists():
                        fence = _read_json(self.fence, max_bytes=16 * 1024)
                        fence_tx = fence.get("transaction_id")
                        fenced = (
                            isinstance(fence_tx, str)
                            and fence_tx.replace(":", "_") == tx.name
                        )
                    if fenced:
                        _fail(
                            "recovery_required",
                            "fenced publication transaction journal is missing",
                        )
                    shutil.rmtree(tx)
                    recovered.append(
                        {"transaction_id": tx.name, "outcome": "abandoned"}
                    )
                    continue
                journal = _read_json(journal_path)
                stage = journal.get("stage")
                if stage in ("committed", "rolled-back"):
                    continue
                if (
                    journal.get("new_state_hash")
                    and journal["new_state_hash"] == current_hash
                ):
                    journal["stage"] = "committed"
                    self._write_journal(tx, journal)
                    recovered.append(
                        {
                            "transaction_id": journal["transaction_id"],
                            "outcome": "committed",
                        }
                    )
                else:
                    self._rollback(tx, journal)
                    recovered.append(
                        {
                            "transaction_id": journal["transaction_id"],
                            "outcome": "rolled-back",
                        }
                    )
            if self.fence.exists():
                active = [
                    path
                    for path in self._transaction_directories()
                    if _read_json(path / "journal.json").get("stage")
                    not in ("committed", "rolled-back")
                ]
                if not active:
                    self.fence.unlink()
                    _fsync_directory(self.fence.parent)
            return recovered

    def apply(self, request: Any, plan: Any) -> dict[str, Any]:
        self.recover()
        shaped = self._normalize_request_shape(request)
        replay_hash = _sha(canonical_bytes(shaped))
        replay_target = shaped["draft"]["target_id"]
        self.capability.require("reviewed-publisher", replay_target)
        with _writer_lock(self.data_root, exclusive=False):
            assert_publication_stable(self.data_root)
            replay_state, _ = self._current_state()
            existing = replay_state["idempotency"].get(shaped["idempotency_key"])
            if existing:
                if existing["request_hash"] != replay_hash:
                    _fail(
                        "idempotency_conflict",
                        "idempotency key was used for different content",
                    )
                replay_receipt = replay_state["receipts"].get(existing["receipt_id"])
                if (
                    not replay_receipt
                    or replay_receipt["receipt_hash"] != existing["receipt_hash"]
                ):
                    _fail(
                        "state_corrupt",
                        "idempotency record does not bind a valid receipt",
                    )
                self._effect_truth(
                    replay_target, replay_state["targets"][replay_target]
                )
                return cast(dict[str, Any], replay_receipt)
        instant = self._trusted_now()
        normalized, preview, consent_hash, review_hash = self._validate_request(
            request, instant
        )
        draft = normalized["draft"]
        self.capability.require("reviewed-publisher", draft["target_id"])
        current_plan = self._plan_from_validated(
            normalized, preview, consent_hash, review_hash
        )
        self._validate_plan(plan, current_plan)
        target_id = draft["target_id"]
        request_hash = current_plan["body"]["request_hash"]
        rendered = _unb64u(preview["body"]["rendered"]["bytes_b64"])
        with _writer_lock(self.data_root, exclusive=True):
            state, state_hash = self._current_state()
            existing = state["idempotency"].get(normalized["idempotency_key"])
            if existing:
                if existing["request_hash"] != request_hash:
                    _fail(
                        "idempotency_conflict",
                        "idempotency key was used for different content",
                    )
                locked_replay_receipt = state["receipts"].get(existing["receipt_id"])
                if (
                    not locked_replay_receipt
                    or locked_replay_receipt["receipt_hash"] != existing["receipt_hash"]
                ):
                    _fail(
                        "state_corrupt",
                        "idempotency record does not bind a valid receipt",
                    )
                self._effect_truth(target_id, state["targets"][target_id])
                return cast(dict[str, Any], locked_replay_receipt)
            before = self._before(state, target_id)
            if before != current_plan["body"]["before"]:
                _fail("target_drift", "publication target changed after planning")
            prior_attempts = 0
            for prior in self._transaction_directories():
                if not (prior / "journal.json").is_file():
                    continue
                prior_journal = _read_json(prior / "journal.json")
                if prior_journal.get("request_hash") == request_hash:
                    prior_attempts += 1
            transaction_id, _ = _content_id(
                "cm:publication-transaction:v1",
                {
                    "request_hash": request_hash,
                    "at": _format_timestamp(instant),
                    "state_hash": state_hash,
                    "attempt": prior_attempts + 1,
                },
            )
            tx = self.transactions / transaction_id.replace(":", "_")
            if tx.exists():
                _fail(
                    "transaction_collision",
                    "publication transaction identity already exists",
                )
            tx.mkdir(mode=0o700)
            relative = self.config.targets[target_id]
            target = _resolved_file(self.root, relative, must_exist=False)
            target_existed = target.is_file() and not target.is_symlink()
            created_parent_paths: list[str] = []
            parent = target.parent
            while parent != self.root and not parent.exists():
                created_parent_paths.append(parent.relative_to(self.root).as_posix())
                parent = parent.parent
            try:
                if target_existed:
                    _atomic_bytes(tx / "target.before", target.read_bytes())
                self.runner.snapshot(tx)
            except ExchangeError:
                shutil.rmtree(tx, ignore_errors=True)
                raise
            except Exception:  # noqa: BLE001 - close every host failure at boundary
                shutil.rmtree(tx, ignore_errors=True)
                _fail(
                    "snapshot_failed",
                    "publication rollback snapshot could not be prepared",
                )
            journal: dict[str, Any] = {
                "schema": "collective-publication-journal/v1",
                "transaction_id": transaction_id,
                "request_hash": request_hash,
                "idempotency_key": normalized["idempotency_key"],
                "target_id": target_id,
                "relative_path": relative,
                "target_existed": target_existed,
                "created_parent_paths": created_parent_paths,
                "new_state_hash": None,
                "stage": "snapshot-staged",
            }
            self._write_journal(tx, journal)
            _atomic_json(
                self.fence,
                {
                    "schema": "collective-publication-fence/v1",
                    "transaction_id": transaction_id,
                    "stage": "snapshot-staged",
                },
            )
            self.fault_hook("snapshot-staged")
            journal["stage"] = "prepared"
            self._write_journal(tx, journal)
            _atomic_json(
                self.fence,
                {
                    "schema": "collective-publication-fence/v1",
                    "transaction_id": transaction_id,
                    "stage": "prepared",
                },
            )
            self.fault_hook("prepared")
            receipt: dict[str, Any] | None = None
            try:
                self._install_target(target, rendered)
                journal["stage"] = "target-published"
                self._write_journal(tx, journal)
                _atomic_json(
                    self.fence,
                    {
                        "schema": "collective-publication-fence/v1",
                        "transaction_id": transaction_id,
                        "stage": journal["stage"],
                    },
                )
                self.fault_hook("target-published")
                self.runner.build()
                projection = self.runner.verify(relative, _sha(rendered))
                journal["stage"] = "projections-published"
                self._write_journal(tx, journal)
                self.fault_hook("projections-published")
                committed_at = _format_timestamp(instant)
                receipt_body = {
                    "transaction_id": transaction_id,
                    "request_hash": request_hash,
                    "plan_id": current_plan["plan_id"],
                    "idempotency_key": normalized["idempotency_key"],
                    "target_id": target_id,
                    "action": draft["action"],
                    "before": before,
                    "after": current_plan["body"]["after"],
                    "source_refs": draft["source_refs"],
                    "source_checkpoint": draft["source_checkpoint"],
                    "classification": draft["classification"],
                    "policy_version": draft["policy_version"],
                    "consent": {
                        "evidence_id": normalized["consent"]["body"]["evidence_id"],
                        "evidence_hash": consent_hash,
                    },
                    "review": {
                        "evidence_id": normalized["review"]["body"]["evidence_id"],
                        "evidence_hash": review_hash,
                    },
                    "projection": projection,
                    "committed_at": committed_at,
                    "status": "committed",
                }
                receipt_id, receipt_hash = _content_id(
                    "cm:publication-receipt:v1", receipt_body
                )
                receipt = {
                    "schema": PUBLICATION_RECEIPT_SCHEMA,
                    "receipt_id": receipt_id,
                    "receipt_hash": receipt_hash,
                    "body": receipt_body,
                }
                target_record = {
                    "receipt_id": receipt_id,
                    "receipt_hash": receipt_hash,
                    "content_hash": current_plan["body"]["after"]["content_hash"],
                    "content_length": current_plan["body"]["after"]["content_length"],
                    "state": current_plan["body"]["after"]["state"],
                }
                new_state = {
                    "schema": PUBLICATION_STATE_SCHEMA,
                    "generation": int(state["generation"]) + 1,
                    "previous_state_hash": state_hash,
                    "targets": dict(state["targets"]) | {target_id: target_record},
                    "idempotency": dict(state["idempotency"])
                    | {
                        normalized["idempotency_key"]: {
                            "request_hash": request_hash,
                            "receipt_id": receipt_id,
                            "receipt_hash": receipt_hash,
                        }
                    },
                    "receipts": dict(state["receipts"]) | {receipt_id: receipt},
                }
                new_state_hash = _sha(canonical_bytes(new_state))
                generation = self.generations / new_state_hash
                if generation.exists() or generation.is_symlink():
                    if generation.is_symlink() or not generation.is_dir():
                        _fail(
                            "state_corrupt",
                            "publication generation path is unsafe",
                        )
                    if (
                        _read_json(generation / "state.json") != new_state
                        or _read_json(generation / "receipt.json") != receipt
                    ):
                        _fail(
                            "state_corrupt",
                            "publication generation identity collision",
                        )
                else:
                    staged_generation = tx / "state-generation"
                    staged_generation.mkdir(mode=0o700)
                    _atomic_json(staged_generation / "state.json", new_state)
                    _atomic_json(staged_generation / "receipt.json", receipt)
                    _fsync_directory(staged_generation)
                    os.rename(staged_generation, generation)
                    _fsync_directory(self.generations)
                journal["new_state_hash"] = new_state_hash
                journal["stage"] = "receipt-staged"
                self._write_journal(tx, journal)
                self.fault_hook("receipt-staged")
                _atomic_symlink(self.state_root / "current", generation)
                journal["stage"] = "state-published"
                self._write_journal(tx, journal)
                self.fault_hook("state-published")
                journal["stage"] = "committed"
                self._write_journal(tx, journal)
                try:
                    self.fence.unlink()
                    _fsync_directory(self.fence.parent)
                except FileNotFoundError:
                    pass
                self.fault_hook("journal-committed")
                return receipt
            except ExchangeError:
                _, observed_hash = self._current_state()
                if (
                    journal.get("new_state_hash") == observed_hash
                    and receipt is not None
                ):
                    journal["stage"] = "committed"
                    self._write_journal(tx, journal)
                    try:
                        self.fence.unlink()
                        _fsync_directory(self.fence.parent)
                    except FileNotFoundError:
                        pass
                    self._effect_truth(
                        target_id, self._current_state()[0]["targets"][target_id]
                    )
                    return receipt
                self._rollback(tx, journal)
                raise
            except Exception:  # noqa: BLE001 - rollback closes arbitrary provider failures
                _, observed_hash = self._current_state()
                if (
                    journal.get("new_state_hash") == observed_hash
                    and receipt is not None
                ):
                    journal["stage"] = "committed"
                    self._write_journal(tx, journal)
                    try:
                        self.fence.unlink()
                        _fsync_directory(self.fence.parent)
                    except FileNotFoundError:
                        pass
                    self._effect_truth(
                        target_id, self._current_state()[0]["targets"][target_id]
                    )
                    return receipt
                self._rollback(tx, journal)
                _fail(
                    "publication_failed",
                    "publication transaction failed and was rolled back",
                )

    def reconcile(self, receipt_id: str) -> dict[str, Any]:
        self.capability.require_role("reviewed-publisher")
        _identifier(receipt_id, "receipt_id")
        self.recover()
        with _writer_lock(self.data_root, exclusive=False):
            state, state_hash = self._current_state()
            receipt = state["receipts"].get(receipt_id)
            if receipt is None:
                _fail("unknown_receipt", "publication receipt is not accepted")
            target_id = receipt["body"]["target_id"]
            self.capability.require("reviewed-publisher", target_id)
            projection = self._effect_truth(target_id, state["targets"][target_id])
            return {
                "schema": "collective-publication-reconciliation/v1",
                "receipt_id": receipt_id,
                "receipt_hash": receipt["receipt_hash"],
                "state_hash": state_hash,
                "effect": "verified",
                "projection": projection,
            }
