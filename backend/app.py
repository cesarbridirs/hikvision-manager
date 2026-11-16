import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from credentials import CredentialStore
from check_database import check_integrity


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DB_PATH = ROOT / "data" / "hikvision.db"
KEY_PATH = Path(os.environ.get("HIKVISION_KEY_FILE", ROOT / "secrets" / "credentials.key"))
CREDENTIALS = None
SCHEMA_VERSION = 3
HOST = os.environ.get("HIKVISION_BIND_HOST", "127.0.0.1")
PORT = 8088
HTTPS_PORT = 8443
HIKVISION_TIMEOUT_SECONDS = 20
NVR_REACHABILITY_TIMEOUT_SECONDS = 3
JOB_LOCK = threading.Lock()
SCHEDULER_WAKE = threading.Event()
SCHEDULER_STATE = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_message": "nao executado",
    "last_summary": None,
}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=10000")
    conn.execute("pragma foreign_keys=ON")
    conn.execute("pragma secure_delete=ON")
    return conn


def init_db():
    global CREDENTIALS
    started = time.monotonic()
    print("Inicializacao: verificando integridade do banco...", flush=True)
    needs_migration = False
    legacy_migration = False
    if DB_PATH.exists():
        # Refuse migration before any schema or credential changes on damaged data.
        check_integrity(DB_PATH)
        print(f"Inicializacao: integridade OK em {time.monotonic() - started:.1f}s", flush=True)
        with connect() as conn:
            previous_version = conn.execute("pragma user_version").fetchone()[0]
            needs_migration = previous_version < SCHEMA_VERSION
            legacy_migration = previous_version < 1
            if needs_migration:
                backup_dir = DB_PATH.parent / "backups"
                backup_dir.mkdir(exist_ok=True)
                backup_path = backup_dir / (f"before-v{SCHEMA_VERSION}-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")
                with sqlite3.connect(backup_path, factory=ClosingConnection) as backup:
                    conn.backup(backup)
    with connect() as conn:
        conn.execute("pragma journal_mode=WAL")
        conn.execute("pragma synchronous=NORMAL")
        conn.executescript(
            """
            create table if not exists nvrs (
                id integer primary key autoincrement,
                name text not null,
                base_url text not null unique,
                username text not null,
                password text not null,
                lookback_minutes integer not null default 0,
                nvr_time_offset_hours integer not null default -3,
                enabled integer not null default 1,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists channels (
                id integer primary key autoincrement,
                nvr_id integer not null references nvrs(id) on delete cascade,
                channel integer,
                track_id text not null,
                stream_url text,
                camera_ip text,
                description text,
                channel_state text,
                channel_online integer,
                channel_configured integer,
                last_status text,
                last_message text,
                last_checked_at text,
                last_status_changed_at text,
                last_recording_start text,
                last_recording_end text,
                enabled integer not null default 1,
                last_discovered_at text not null,
                unique(nvr_id, track_id)
            );

            create table if not exists monitor_results (
                id integer primary key autoincrement,
                nvr_id integer not null references nvrs(id) on delete cascade,
                channel_id integer references channels(id) on delete set null,
                status text not null,
                message text,
                checked_at text not null,
                last_start text,
                last_end text
            );

            create table if not exists camera_ip_changes (
                id integer primary key autoincrement,
                nvr_id integer not null references nvrs(id) on delete cascade,
                channel_id integer references channels(id) on delete set null,
                channel integer,
                track_id text,
                old_ip text,
                new_ip text not null,
                change_type text not null,
                detected_at text not null,
                source text not null
            );

            create table if not exists channel_status_changes (
                id integer primary key autoincrement,
                nvr_id integer not null references nvrs(id) on delete cascade,
                channel_id integer references channels(id) on delete set null,
                channel integer,
                track_id text,
                old_status text,
                new_status text not null,
                old_message text,
                new_message text,
                changed_at text not null,
                source text not null
            );

            create table if not exists hdds (
                id integer primary key autoincrement,
                nvr_id integer not null references nvrs(id) on delete cascade,
                hdd_id text not null,
                name text,
                status text,
                capacity text,
                free_space text,
                property text,
                type text,
                last_checked_at text not null,
                last_status_changed_at text,
                unique(nvr_id, hdd_id)
            );

            create table if not exists locations (
                id integer primary key autoincrement,
                city text not null collate nocase,
                block text not null collate nocase,
                unique(city, block)
            );

            create table if not exists app_settings (
                key text primary key,
                value text not null
            );

            create table if not exists alert_acknowledgements (
                id integer primary key autoincrement,
                alert_type text not null,
                ref_id integer not null,
                status text,
                status_changed_at text,
                acknowledged_at text not null,
                hidden_at text,
                unique(alert_type, ref_id, status, status_changed_at)
            );

            create table if not exists scheduler_runs (
                id integer primary key autoincrement,
                started_at text not null,
                finished_at text,
                status text not null,
                message text,
                discover_summary text,
                monitor_summary text
            );

            create table if not exists app_users (
                id integer primary key autoincrement,
                username text not null unique,
                password_hash text not null,
                salt text not null,
                role text not null default 'admin',
                must_change_password integer not null default 0,
                created_at text not null
            );

            create table if not exists app_sessions (
                token text primary key,
                user_id integer not null references app_users(id) on delete cascade,
                created_at text not null,
                expires_at text not null
            );

            create table if not exists app_user_cities (
                user_id integer not null references app_users(id) on delete cascade,
                city text not null collate nocase,
                primary key(user_id, city)
            );
            """
        )
        ensure_columns(conn)
        conn.execute("update hdds set last_status_changed_at=last_checked_at where last_status_changed_at is null")
        encrypted = conn.execute("select count(*) from nvrs where password_encrypted=1").fetchone()[0]
        CREDENTIALS = CredentialStore(KEY_PATH, allow_create=not encrypted)
        for row in conn.execute("select id, password, password_encrypted from nvrs").fetchall():
            if row["password_encrypted"]:
                CREDENTIALS.decrypt(row["password"])
            else:
                conn.execute("update nvrs set password=?, password_encrypted=1 where id=?",
                             (CREDENTIALS.encrypt(row["password"]), row["id"]))
        # Legacy versions did not enforce foreign keys. Backup precedes cleanup.
        if legacy_migration:
            for table in ("channels", "hdds", "monitor_results", "camera_ip_changes", "channel_status_changes"):
                conn.execute(f"delete from {table} where nvr_id not in (select id from nvrs)")
            for table in ("monitor_results", "camera_ip_changes", "channel_status_changes"):
                conn.execute(f"update {table} set channel_id=null where channel_id not in (select id from channels)")
        ensure_default_settings(conn)
        for alert_type, table in (("channel_status", "channels"), ("ip_change", "camera_ip_changes"), ("status_change", "channel_status_changes"), ("hdd_status", "hdds")):
            if legacy_migration:
                conn.execute(f"delete from alert_acknowledgements where alert_type=? and ref_id not in (select id from {table})", (alert_type,))
            conn.execute(f"""create trigger if not exists cleanup_ack_{table}
                after delete on {table} begin
                delete from alert_acknowledgements where alert_type='{alert_type}' and ref_id=old.id;
                end""")
        ensure_default_user(conn)
        conn.execute("update scheduler_runs set status='interrupted', finished_at=?, message='Execucao interrompida pelo encerramento do servidor' where status='running'", (now_iso(),))
        conn.execute("create index if not exists ix_channels_active on channels(nvr_id, enabled)")
        conn.execute("create index if not exists ix_monitor_time on monitor_results(checked_at)")
        conn.execute(f"pragma user_version={SCHEMA_VERSION}")
    if legacy_migration:
        with connect() as conn:
            conn.execute("pragma wal_checkpoint(TRUNCATE)")
            conn.execute("vacuum")
    print(f"Inicializacao: banco pronto em {time.monotonic() - started:.1f}s", flush=True)


def ensure_columns(conn):
    expected = {
        "nvrs": {
            "location_id": "integer references locations(id) on delete restrict",
            "lookback_minutes": "integer not null default 0",
            "nvr_time_offset_hours": "integer not null default -3",
            "password_encrypted": "integer not null default 0",
        },
        "channels": {
            "channel_state": "text",
            "channel_online": "integer",
            "channel_configured": "integer",
            "last_status": "text",
            "last_message": "text",
            "last_checked_at": "text",
            "last_status_changed_at": "text",
            "last_recording_start": "text",
            "last_recording_end": "text",
        },
        "hdds": {
            "last_status_changed_at": "text",
        },
        "app_users": {
            "must_change_password": "integer not null default 0",
        },
        "alert_acknowledgements": {
            "hidden_at": "text",
        },
    }
    for table, columns in expected.items():
        existing = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"alter table {table} add column {name} {definition}")


def ensure_default_settings(conn):
    defaults = {
        "scheduler_enabled": "1",
        "scheduler_interval_minutes": "30",
        "scheduler_run_discover": "1",
        "scheduler_run_monitor": "1",
        "scheduler_updated_at": "",
        "global_lookback_minutes": "720",
        "https_enabled": "0",
        "https_cert_path": str(ROOT / "certs" / "server.crt"),
        "https_key_path": str(ROOT / "certs" / "server.key"),
        "https_port": str(HTTPS_PORT),
    }
    for key, value in defaults.items():
        conn.execute(
            "insert or ignore into app_settings(key, value) values(?, ?)",
            (key, value),
        )


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
    return digest.hex(), salt


def ensure_default_user(conn):
    exists = conn.execute("select id from app_users where username='admin'").fetchone()
    if exists:
        row = conn.execute("select password_hash, salt, must_change_password from app_users where username='admin'").fetchone()
        default_hash, _ = hash_password("admin", row["salt"])
        if hmac.compare_digest(default_hash, row["password_hash"]) and not row["must_change_password"]:
            conn.execute("update app_users set must_change_password=1 where username='admin'")
        return
    initial_password = os.environ.get("HIKVISION_INITIAL_ADMIN_PASSWORD", "")
    if len(initial_password) < 12:
        raise RuntimeError("Defina HIKVISION_INITIAL_ADMIN_PASSWORD (minimo 12 caracteres) antes da primeira inicializacao.")
    password_hash, salt = hash_password(initial_password)
    conn.execute(
        "insert into app_users(username, password_hash, salt, role, must_change_password, created_at) values(?, ?, ?, ?, ?, ?)",
        ("admin", password_hash, salt, "admin", 1, sao_paulo_iso()),
    )


def get_settings():
    with connect() as conn:
        rows = conn.execute("select key, value from app_settings").fetchall()
    raw = {row["key"]: row["value"] for row in rows}
    return {
        "scheduler_enabled": raw.get("scheduler_enabled", "1") == "1",
        "scheduler_interval_minutes": int(raw.get("scheduler_interval_minutes", "30") or 30),
        "scheduler_run_discover": raw.get("scheduler_run_discover", "1") == "1",
        "scheduler_run_monitor": raw.get("scheduler_run_monitor", "1") == "1",
        "scheduler_updated_at": raw.get("scheduler_updated_at", ""),
        "global_lookback_minutes": int(raw.get("global_lookback_minutes", "720") or 720),
        "https_enabled": raw.get("https_enabled", "1") == "1",
        "https_cert_path": raw.get("https_cert_path", str(ROOT / "certs" / "server.crt")),
        "https_key_path": raw.get("https_key_path", str(ROOT / "certs" / "server.key")),
        "https_port": int(raw.get("https_port", str(HTTPS_PORT)) or HTTPS_PORT),
        "state": SCHEDULER_STATE,
    }


def save_settings(data):
    allowed = {
        "scheduler_enabled",
        "scheduler_interval_minutes",
        "scheduler_run_discover",
        "scheduler_run_monitor",
        "global_lookback_minutes",
        "https_enabled",
        "https_cert_path",
        "https_key_path",
        "https_port",
    }
    with connect() as conn:
        ensure_default_settings(conn)
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool):
                value = "1" if value else "0"
            elif key == "scheduler_interval_minutes":
                value = str(max(1, int(value or 30)))
            elif key == "global_lookback_minutes":
                value = str(max(1, int(value or 720)))
            elif key == "https_port":
                value = str(max(1, int(value or HTTPS_PORT)))
            else:
                value = str(value)
            conn.execute(
                "insert into app_settings(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
                (key, value),
            )
        conn.execute(
            "insert into app_settings(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
            ("scheduler_updated_at", now_iso()),
        )
    SCHEDULER_WAKE.set()
    return get_settings()


def sao_paulo_timezone():
    sao_paulo = timezone(timedelta(hours=-3), "America/Sao_Paulo")
    return sao_paulo


def now_iso():
    return datetime.now(sao_paulo_timezone()).replace(microsecond=0).isoformat()


def sao_paulo_iso():
    return now_iso()


def to_sao_paulo_iso(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(sao_paulo_timezone()).replace(microsecond=0).isoformat()


def normalize_base_url(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("IP/URL vazio")
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    parsed = urllib.parse.urlparse(value)
    host = parsed.hostname or value
    scheme = parsed.scheme or "http"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{host}{port}"


def row_to_dict(row):
    return dict(row) if row else None


def validate_lookback(value):
    minutes = int(value or 0)
    if not 0 <= minutes <= 525600:
        raise ValueError("Busca deve ser 0 (global) ou entre 1 e 525600 minutos")
    return minutes


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, status, text, content_type="text/plain; charset=utf-8"):
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def binary_response(handler, status, body, content_type="application/octet-stream"):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8-sig")
    return json.loads(raw or "{}")


def parse_cookies(header):
    cookies = {}
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def get_session_user(handler):
    token = parse_cookies(handler.headers.get("Cookie")).get("hikvision_session")
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            select u.id, u.username, u.role, u.must_change_password
            from app_sessions s
            join app_users u on u.id = s.user_id
            where s.token=? and s.expires_at > ?
            """,
            (token, sao_paulo_iso()),
        ).fetchone()
    return row_to_dict(row) if row else None


def require_auth(handler):
    if handler.path in ("/api/login", "/api/logout", "/api/change-password"):
        return True
    if not handler.path.startswith("/api/"):
        return True
    if handler.path == "/api/health":
        return True
    user = get_session_user(handler)
    if user:
        if user.get("must_change_password") and handler.path not in ("/api/change-password", "/api/me"):
            json_response(handler, 403, {"error": "troca de senha obrigatoria", "must_change_password": True})
            return False
        return True
    json_response(handler, 401, {"error": "nao autenticado"})
    return False


def require_admin(handler):
    user = get_session_user(handler)
    if user and user["role"] == "admin":
        return True
    json_response(handler, 403, {"error": "acesso permitido apenas ao administrador"})
    return False


def visible_nvr_ids(conn, user):
    if user["role"] == "admin":
        return None
    return {row[0] for row in conn.execute(
        """select n.id from nvrs n
           join locations l on l.id=n.location_id
           join app_user_cities uc on uc.city=l.city and uc.user_id=?
           where n.enabled=1""", (user["id"],))}


def can_view_nvr(conn, user, nvr_id):
    allowed = visible_nvr_ids(conn, user)
    return allowed is None or nvr_id in allowed


def validated_user_cities(conn, cities):
    if not isinstance(cities, list) or any(not isinstance(city, str) for city in cities):
        raise ValueError("Selecione cidades validas.")
    selected = {city.strip().casefold() for city in cities}
    available = {row[0].casefold(): row[0] for row in conn.execute("select distinct city from locations")}
    if not selected or not selected <= available.keys():
        raise ValueError("Selecione ao menos uma cidade cadastrada.")
    return sorted((available[city] for city in selected), key=str.casefold)


def revoke_missing_city(conn, city):
    if not conn.execute("select 1 from locations where city=? limit 1", (city,)).fetchone():
        conn.execute("delete from app_user_cities where city=?", (city,))


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    created = sao_paulo_iso()
    expires = (datetime.now(timezone(timedelta(hours=-3))) + timedelta(hours=12)).replace(microsecond=0).isoformat()
    with connect() as conn:
        conn.execute(
            "insert into app_sessions(token, user_id, created_at, expires_at) values(?, ?, ?, ?)",
            (token, user_id, created, expires),
        )
    return token


def clear_session(handler):
    token = parse_cookies(handler.headers.get("Cookie")).get("hikvision_session")
    if token:
        with connect() as conn:
            conn.execute("delete from app_sessions where token=?", (token,))


def change_password(handler, data):
    user = get_session_user(handler)
    if not user:
        raise ValueError("nao autenticado")
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""
    if len(new_password) < 8:
        raise ValueError("a nova senha deve ter pelo menos 8 caracteres")
    with connect() as conn:
        row = conn.execute("select * from app_users where id=?", (user["id"],)).fetchone()
        current_hash, _ = hash_password(current_password, row["salt"])
        if not hmac.compare_digest(current_hash, row["password_hash"]):
            raise ValueError("senha atual invalida")
        password_hash, salt = hash_password(new_password)
        conn.execute(
            "update app_users set password_hash=?, salt=?, must_change_password=0 where id=?",
            (password_hash, salt, user["id"]),
        )


def make_digest_opener(base_url, username, password):
    password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_mgr.add_password(None, base_url, username, password)
    auth_handler = urllib.request.HTTPDigestAuthHandler(password_mgr)
    return urllib.request.build_opener(auth_handler)


class NvrUnavailable(RuntimeError):
    pass


class NvrAuthenticationError(NvrUnavailable):
    pass


def nvr_password(nvr):
    if nvr["password_encrypted"]:
        return CREDENTIALS.decrypt(nvr["password"])
    raise RuntimeError("Credencial nao migrada; reinicie o servidor.")


def active_nvrs(specific_nvr_id=None):
    with connect() as conn:
        if specific_nvr_id is not None:
            rows = conn.execute("select * from nvrs where id=? and enabled=1", (specific_nvr_id,)).fetchall()
        else:
            rows = conn.execute("select * from nvrs where enabled=1 order by name").fetchall()
    return [dict(row) for row in rows]


def nvr_reachability_target(nvr):
    parsed = urllib.parse.urlparse(nvr["base_url"])
    host = parsed.hostname
    if not host:
        raise RuntimeError("URL do NVR invalida")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def check_nvr_reachable(nvr, timeout=NVR_REACHABILITY_TIMEOUT_SECONDS):
    host, port = nvr_reachability_target(nvr)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except Exception as exc:
        return False, f"NVR indisponivel ({host}:{port}): {exc}"


def ensure_nvr_reachable(nvr):
    reachable, error = check_nvr_reachable(nvr)
    if not reachable:
        raise NvrUnavailable(error)


def hikvision_request(nvr, path, method="GET", body=None, content_type="application/xml", timeout=HIKVISION_TIMEOUT_SECONDS):
    base_url = nvr["base_url"]
    url = base_url.rstrip("/") + path
    opener = make_digest_opener(base_url, nvr["username"], nvr_password(nvr))
    data = None if body is None else body.encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/xml, text/xml, application/json")
    if data is not None:
        request.add_header("Content-Type", content_type)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code in (401, 403):
            raise NvrAuthenticationError(f"NVR recusou autenticacao/permissao (HTTP {exc.code})") from exc
        if exc.code >= 500:
            raise NvrUnavailable(f"NVR indisponivel (HTTP {exc.code})") from exc
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}")
    except Exception as exc:
        raise NvrUnavailable(f"Falha de comunicacao com NVR: {exc}") from exc


def hikvision_binary_request(nvr, path, timeout=HIKVISION_TIMEOUT_SECONDS):
    base_url = nvr["base_url"]
    url = base_url.rstrip("/") + path
    opener = make_digest_opener(base_url, nvr["username"], nvr_password(nvr))
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "image/jpeg,image/*,*/*")
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            return response.status, response.read(), content_type
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}")
    except Exception as exc:
        raise RuntimeError(str(exc))


def fetch_channel_snapshot(nvr, channel):
    ensure_nvr_reachable(nvr)
    track_id = str(channel["track_id"] or "").strip()
    if not track_id:
        raise RuntimeError("canal sem trackID")
    paths = (
        f"/ISAPI/Streaming/channels/{urllib.parse.quote(track_id)}/picture",
        f"/ISAPI/Streaming/channels/{urllib.parse.quote(track_id)}/picture?snapShotImageType=JPEG",
        f"/Streaming/channels/{urllib.parse.quote(track_id)}/picture",
    )
    errors = []
    for path in paths:
        try:
            _, body, content_type = hikvision_binary_request(nvr, path, timeout=8)
            if body.lstrip().startswith(b"<?xml") or body.lstrip().startswith(b"{"):
                errors.append(f"{path}: resposta nao e imagem")
                continue
            return body, content_type or "image/jpeg"
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    raise RuntimeError("falha ao obter imagem; tentativas: " + " | ".join(errors))


def xml_text(node, name):
    found = node.find(f".//{{*}}{name}")
    return found.text.strip() if found is not None and found.text else ""


def parse_track_list(xml_text_value):
    root = ET.fromstring(xml_text_value)
    if root.tag.rsplit("}", 1)[-1] != "TrackList":
        raise ValueError("Resposta de descoberta invalida; canais anteriores preservados")
    tracks = []
    for track in root.findall(".//{*}Track"):
        track_id = xml_text(track, "id") or xml_text(track, "Channel")
        src_channel = xml_text(track, "SrcChannel")
        src_url = xml_text(track, "SrcUrl")
        description = xml_text(track, "Description")
        channel = None
        if src_channel.isdigit():
            channel = int(src_channel)
        elif track_id and track_id[:-2].isdigit():
            channel = int(track_id[:-2])
        tracks.append(
            {
                "track_id": track_id,
                "channel": channel,
                "stream_url": src_url,
                "description": description,
            }
        )
    return tracks


def parse_channel_statuses(xml_text_value):
    statuses = {}
    root = ET.fromstring(xml_text_value)
    for node in root.iter():
        short_name = node.tag.rsplit("}", 1)[-1]
        if short_name not in ("InputProxyChannelStatus", "InputProxyChannel"):
            continue
        channel_text = xml_text(node, "id") or xml_text(node, "channelID")
        if not channel_text.isdigit():
            continue
        online_text = xml_text(node, "online") or xml_text(node, "Online")
        status_text = xml_text(node, "status") or xml_text(node, "Status")
        ip_address = xml_text(node, "ipAddress") or xml_text(node, "ip") or xml_text(node, "IPAddress")
        online = None
        if online_text:
            online = online_text.lower() == "true"
        elif status_text:
            online = status_text.lower() in ("online", "connected", "normal")
        statuses[int(channel_text)] = {
            "online": online,
            "status": status_text or ("online" if online else "offline" if online is False else None),
            "ip_address": ip_address or None,
        }
    return statuses


def fetch_channel_statuses(nvr):
    merged = {}
    successful = 0
    for path in (
        "/ISAPI/ContentMgmt/InputProxy/channels/status",
        "/ISAPI/ContentMgmt/InputProxy/channels",
    ):
        try:
            _, xml = hikvision_request(nvr, path)
            root = ET.fromstring(xml)
            if root.tag.rsplit("}", 1)[-1] not in ("InputProxyChannelStatusList", "InputProxyChannelList"):
                raise ValueError("Resposta de estado dos canais invalida")
            for channel, status in parse_channel_statuses(xml).items():
                current = merged.get(channel, {})
                current.update({key: value for key, value in status.items() if value not in (None, "")})
                merged[channel] = current
            successful += 1
        except NvrUnavailable:
            raise
        except Exception:
            continue
    if not successful:
        raise RuntimeError("Nao foi possivel consultar o estado dos canais; dados anteriores preservados")
    return merged


def to_hikvision_time(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def apply_hour_offset(dt, hours):
    from datetime import timedelta

    return dt + timedelta(hours=hours or 0)


def build_search_xml(track_id, start, end):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <searchID>{{00000000-0000-0000-0000-000000000001}}</searchID>
  <trackList>
    <trackID>{track_id}</trackID>
  </trackList>
  <timeSpanList>
    <timeSpan>
      <startTime>{to_hikvision_time(start)}</startTime>
      <endTime>{to_hikvision_time(end)}</endTime>
    </timeSpan>
  </timeSpanList>
  <maxResults>1</maxResults>
  <searchResultPostion>0</searchResultPostion>
  <metadataList>
    <metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
  </metadataList>
</CMSearchDescription>"""


def parse_search_result(xml_text_value):
    root = ET.fromstring(xml_text_value)
    matches_text = xml_text(root, "numOfMatches")
    try:
        matches = int(matches_text)
    except Exception:
        matches = None
    start_time = xml_text(root, "startTime")
    end_time = xml_text(root, "endTime")
    return {
        "matches": matches,
        "start_time": to_sao_paulo_iso(start_time),
        "end_time": to_sao_paulo_iso(end_time),
    }


def parse_hdd_list(xml_text_value):
    root = ET.fromstring(xml_text_value)
    hdds = []
    for node in root.iter():
        short_name = node.tag.rsplit("}", 1)[-1].lower()
        if short_name not in ("hdd", "storagevolume", "volume"):
            continue
        hdd_id = (
            xml_text(node, "id")
            or xml_text(node, "hddID")
            or xml_text(node, "volumeID")
            or xml_text(node, "diskID")
        )
        status = (
            xml_text(node, "status")
            or xml_text(node, "Status")
            or xml_text(node, "health")
            or xml_text(node, "state")
        )
        name = xml_text(node, "name") or xml_text(node, "hddName") or xml_text(node, "volumeName")
        capacity = xml_text(node, "capacity") or xml_text(node, "totalSpace") or xml_text(node, "totalCapacity")
        free_space = xml_text(node, "freeSpace") or xml_text(node, "free") or xml_text(node, "freeCapacity")
        property_value = xml_text(node, "property") or xml_text(node, "Property")
        type_value = xml_text(node, "type") or xml_text(node, "diskType")
        if hdd_id or status or capacity:
            hdds.append(
                {
                    "hdd_id": hdd_id or str(len(hdds) + 1),
                    "name": name,
                    "status": status,
                    "capacity": capacity,
                    "free_space": free_space,
                    "property": property_value,
                    "type": type_value,
                }
            )
    return hdds


def fetch_hdds(nvr):
    paths = (
        "/ISAPI/ContentMgmt/Storage/hdd",
        "/ISAPI/ContentMgmt/Storage",
        "/ISAPI/System/Storage/volumes",
    )
    last_error = None
    for path in paths:
        try:
            _, xml = hikvision_request(nvr, path)
            hdds = parse_hdd_list(xml)
            if hdds:
                return hdds, path
            root_name = ET.fromstring(xml).tag.rsplit("}", 1)[-1].lower()
            if root_name in ("hddlist", "storagevolumelist", "volumelist"):
                return [], path
        except NvrUnavailable:
            raise
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "nenhum HDD retornado")


def hdd_is_ok(status):
    normalized = (status or "").strip().lower()
    return normalized in ("ok", "normal", "healthy", "rw", "readwrite", "working", "idle", "notexist")


def save_nvr_hdds(conn, nvr, hdds, source):
    checked_at = datetime.now(sao_paulo_timezone()).isoformat(timespec="microseconds")
    seen_ids = set()
    for hdd in hdds:
        seen_ids.add(str(hdd["hdd_id"]))
        conn.execute(
            """
            insert into hdds(
                nvr_id, hdd_id, name, status, capacity, free_space,
                property, type, last_checked_at, last_status_changed_at
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(nvr_id, hdd_id) do update set
                name=excluded.name,
                status=excluded.status,
                capacity=excluded.capacity,
                free_space=excluded.free_space,
                property=excluded.property,
                type=excluded.type,
                last_checked_at=excluded.last_checked_at,
                last_status_changed_at=case
                    when coalesce(hdds.status, '') != coalesce(excluded.status, '')
                    then excluded.last_status_changed_at
                    else hdds.last_status_changed_at
                end
            """,
            (
                nvr["id"],
                str(hdd["hdd_id"]),
                hdd["name"],
                hdd["status"],
                hdd["capacity"],
                hdd["free_space"],
                hdd["property"],
                hdd["type"],
                checked_at,
                checked_at,
            ),
        )
    for row in conn.execute("select hdd_id from hdds where nvr_id=?", (nvr["id"],)).fetchall():
        if row["hdd_id"] not in seen_ids:
            conn.execute(
                """update hdds set status='missing', last_checked_at=?,
                   last_status_changed_at=case when status='missing'
                       then last_status_changed_at else ? end
                   where nvr_id=? and hdd_id=?""",
                (checked_at, checked_at, nvr["id"], row["hdd_id"]),
            )
    return {"nvr": nvr["name"], "status": "ok", "source": source, "hdds": len(hdds)}


def update_all_hdds_job(specific_nvr_id=None, blocked=None):
    blocked = blocked if blocked is not None else {}
    items = []
    for nvr in active_nvrs(specific_nvr_id):
        try:
            if nvr["id"] in blocked:
                raise NvrUnavailable(blocked[nvr["id"]])
            ensure_nvr_reachable(nvr)
            hdds, source = fetch_hdds(nvr)
            with connect() as conn:
                if not conn.execute("select id from nvrs where id=?", (nvr["id"],)).fetchone():
                    continue
                items.append(save_nvr_hdds(conn, nvr, hdds, source))
        except Exception as exc:
            items.append({"nvr": nvr["name"], "status": "error", "message": str(exc)[:300], "hdds": 0})
    return {"summary": {
        "nvrs": len(items), "ok": sum(i["status"] == "ok" for i in items),
        "error": sum(i["status"] == "error" for i in items),
        "hdds": sum(i["hdds"] for i in items)}, "items": items}


def channel_is_offline(channel):
    state = (channel["channel_state"] or "").lower()
    return state == "offline" or channel["channel_online"] == 0


def status_label(status):
    return {
        "ok": "OK",
        "no_recording": "SEM_GRAVACAO",
        "offline": "OFFLINE",
        "error": "ERRO",
    }.get(status, status.upper())


def register_camera_ip_change(conn, *, nvr_id, channel_id, channel, track_id, old_ip, new_ip, source):
    old_ip = (old_ip or "").strip()
    new_ip = (new_ip or "").strip()
    if not new_ip or old_ip == new_ip:
        return
    change_type = "new_ip" if not old_ip else "ip_replaced"
    conn.execute(
        """
        insert into camera_ip_changes(
            nvr_id, channel_id, channel, track_id, old_ip, new_ip,
            change_type, detected_at, source
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nvr_id,
            channel_id,
            channel,
            track_id,
            old_ip or None,
            new_ip,
            change_type,
            now_iso(),
            source,
        ),
    )


def register_camera_removed(conn, *, nvr_id, channel_id, channel, track_id, old_ip, source):
    old_ip = (old_ip or "").strip()
    if not old_ip:
        return
    conn.execute(
        """
        insert into camera_ip_changes(
            nvr_id, channel_id, channel, track_id, old_ip, new_ip,
            change_type, detected_at, source
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nvr_id,
            channel_id,
            channel,
            track_id,
            old_ip,
            "-",
            "ip_removed",
            sao_paulo_iso(),
            source,
        ),
    )


def register_status_change(conn, *, nvr_id, channel_id, channel, track_id, old_status, new_status, old_message, new_message, source):
    old_status = (old_status or "").strip()
    new_status = (new_status or "").strip()
    if not new_status or old_status == new_status:
        return
    if old_status != "offline" and new_status != "offline":
        return
    conn.execute(
        """
        insert into channel_status_changes(
            nvr_id, channel_id, channel, track_id, old_status, new_status,
            old_message, new_message, changed_at, source
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nvr_id,
            channel_id,
            channel,
            track_id,
            old_status or None,
            new_status,
            old_message,
            new_message,
            sao_paulo_iso(),
            source,
        ),
    )


def acknowledge_alert(data):
    alert_type = data.get("alert_type")
    ref_id = int(data.get("ref_id") or 0)
    status = data.get("status") or ""
    status_changed_at = data.get("status_changed_at") or ""
    if alert_type not in ("channel_status", "ip_change", "status_change", "hdd_status") or ref_id <= 0:
        raise ValueError("alerta invalido")
    with connect() as conn:
        conn.execute(
            """
            insert or ignore into alert_acknowledgements(
                alert_type, ref_id, status, status_changed_at, acknowledged_at
            ) values(?, ?, ?, ?, ?)
            """,
            (alert_type, ref_id, status, status_changed_at, now_iso()),
        )


def check_channel_recording(nvr, channel, global_lookback_minutes=720):
    checked_at = now_iso()
    if channel_is_offline(channel):
        return {
            "status": "offline",
            "message": "camera offline",
            "checked_at": checked_at,
            "matches": 0,
            "last_start": None,
            "last_end": None,
        }

    from datetime import timedelta

    end = datetime.now(timezone.utc)
    lookback_minutes = int(nvr["lookback_minutes"] or global_lookback_minutes or 720)
    start = end - timedelta(minutes=lookback_minutes)
    search_start = apply_hour_offset(start, int(nvr["nvr_time_offset_hours"] or 0))
    search_end = apply_hour_offset(end, int(nvr["nvr_time_offset_hours"] or 0))
    xml = build_search_xml(channel["track_id"], search_start, search_end)
    try:
        _, response_xml = hikvision_request(nvr, "/ISAPI/ContentMgmt/search", method="POST", body=xml)
        parsed = parse_search_result(response_xml)
        if parsed["matches"] is None:
            return {
                "status": "error",
                "message": "resposta sem numOfMatches",
                "checked_at": checked_at,
                "matches": 0,
                "last_start": None,
                "last_end": None,
            }
        has_recording = parsed["matches"] > 0
        return {
            "status": "ok" if has_recording else "no_recording",
            "message": "gravacao recente encontrada" if has_recording else "camera online sem gravacao recente",
            "checked_at": checked_at,
            "matches": parsed["matches"],
            "last_start": parsed["start_time"],
            "last_end": parsed["end_time"],
        }
    except NvrUnavailable:
        raise
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc)[:300],
            "checked_at": checked_at,
            "matches": 0,
            "last_start": None,
            "last_end": None,
        }


def save_discovered_channels(conn, nvr, tracks, statuses, source):
    timestamp = now_iso()
    seen_track_ids = set()
    for track in tracks:
        seen_track_ids.add(str(track["track_id"]))
        channel_status = statuses.get(track["channel"]) or {}
        online = channel_status.get("online")
        new_camera_ip = channel_status.get("ip_address")
        existing = conn.execute(
            "select id, camera_ip from channels where nvr_id=? and track_id=?",
            (nvr["id"], track["track_id"]),
        ).fetchone()
        register_camera_ip_change(
            conn,
            nvr_id=nvr["id"],
            channel_id=existing["id"] if existing else None,
            channel=track["channel"],
            track_id=track["track_id"],
            old_ip=existing["camera_ip"] if existing else None,
            new_ip=new_camera_ip,
            source=source,
        )
        conn.execute(
            """
            insert into channels(
                nvr_id, channel, track_id, stream_url, camera_ip, description,
                channel_state, channel_online, channel_configured, last_discovered_at
            )
            values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(nvr_id, track_id) do update set
                channel=excluded.channel,
                stream_url=excluded.stream_url,
                camera_ip=coalesce(excluded.camera_ip, channels.camera_ip),
                description=excluded.description,
                channel_state=coalesce(excluded.channel_state, channels.channel_state),
                channel_online=coalesce(excluded.channel_online, channels.channel_online),
                channel_configured=coalesce(excluded.channel_configured, channels.channel_configured),
                enabled=1,
                last_discovered_at=excluded.last_discovered_at
            """,
            (
                nvr["id"],
                track["channel"],
                track["track_id"],
                track["stream_url"],
                new_camera_ip,
                track["description"],
                channel_status.get("status"),
                None if online is None else 1 if online else 0,
                None if new_camera_ip is None else 1 if new_camera_ip else 0,
                timestamp,
            ),
        )
    existing_channels = conn.execute(
        "select id, channel, track_id, camera_ip from channels where nvr_id=? and enabled=1",
        (nvr["id"],),
    ).fetchall()
    for existing in existing_channels:
        if str(existing["track_id"]) in seen_track_ids:
            continue
        register_camera_removed(
            conn,
            nvr_id=nvr["id"],
            channel_id=existing["id"],
            channel=existing["channel"],
            track_id=existing["track_id"],
            old_ip=existing["camera_ip"],
            source=source,
        )
        conn.execute(
            """
            update channels
            set enabled=0,
                channel_state='removed',
                channel_online=0,
                channel_configured=0,
                last_status='removed',
                last_message='camera removida do canal',
                last_checked_at=?,
                last_status_changed_at=?,
                camera_ip=null
            where id=?
            """,
            (sao_paulo_iso(), sao_paulo_iso(), existing["id"]),
        )
    return tracks


def discover_nvr_channels(nvr, source="discover"):
    ensure_nvr_reachable(nvr)
    _, xml = hikvision_request(nvr, "/ISAPI/ContentMgmt/record/tracks")
    tracks = parse_track_list(xml)
    if any(not str(track["track_id"]).isdigit() for track in tracks):
        raise ValueError("TrackID invalido na descoberta")
    statuses = fetch_channel_statuses(nvr)
    with connect() as conn:
        if not conn.execute("select id from nvrs where id=?", (nvr["id"],)).fetchone():
            raise ValueError("NVR excluido durante a consulta")
        return save_discovered_channels(conn, nvr, tracks, statuses, source)


def discover_all_channels_job(specific_nvr_id=None, source="scheduler_discover", blocked=None):
    blocked = blocked if blocked is not None else {}
    items = []
    for nvr in active_nvrs(specific_nvr_id):
        try:
            tracks = discover_nvr_channels(nvr, source=source)
            items.append({"nvr": nvr["name"], "status": "ok", "discovered": len(tracks)})
        except Exception as exc:
            blocked[nvr["id"]] = str(exc)[:300]
            items.append({"nvr": nvr["name"], "status": "error", "message": str(exc)[:300], "discovered": 0})
    return {"summary": {
        "nvrs": len(items), "ok": sum(i["status"] == "ok" for i in items),
        "error": sum(i["status"] == "error" for i in items),
        "discovered": sum(i["discovered"] for i in items)}, "items": items}


def verify_now_job(specific_nvr_id=None):
    blocked = {}
    discover = discover_all_channels_job(specific_nvr_id, source="manual_verify_discover", blocked=blocked)
    monitor = monitor_channels_job(specific_nvr_id, blocked=blocked)
    hdds = update_all_hdds_job(specific_nvr_id, blocked=blocked)
    return {"discover": discover["summary"], "monitor": monitor["summary"],
            "hdds": hdds["summary"], "discover_items": discover["items"],
            "monitor_items": monitor["items"], "hdd_items": hdds["items"]}


def error_result(message):
    return {"status": "error", "message": message[:300], "checked_at": now_iso(),
            "matches": 0, "last_start": None, "last_end": None}


def save_channel_result(conn, nvr, channel, result):
    current = conn.execute("select * from channels where id=? and enabled=1", (channel["id"],)).fetchone()
    if not current:
        return None
    status_changed_at = current["last_status_changed_at"]
    if current["last_status"] != result["status"]:
        status_changed_at = result["checked_at"]
        if result["status"] != "error" and current["last_status"] != "error":
            register_status_change(
                conn, nvr_id=nvr["id"], channel_id=channel["id"], channel=channel["channel"],
                track_id=channel["track_id"], old_status=current["last_status"],
                new_status=result["status"], old_message=current["last_message"],
                new_message=result["message"], source="monitor",
            )
    conn.execute(
        """update channels set last_status=?, last_message=?, last_checked_at=?,
           last_status_changed_at=?, last_recording_start=coalesce(?, last_recording_start),
           last_recording_end=coalesce(?, last_recording_end) where id=?""",
        (result["status"], result["message"], result["checked_at"], status_changed_at,
         result["last_start"], result["last_end"], channel["id"]),
    )
    conn.execute(
        """insert into monitor_results(nvr_id,channel_id,status,message,checked_at,last_start,last_end)
           values(?,?,?,?,?,?,?)""",
        (nvr["id"], channel["id"], result["status"], result["message"],
         result["checked_at"], result["last_start"], result["last_end"]),
    )
    return {"nvr": nvr["name"], "channel": channel["channel"], "track_id": channel["track_id"],
            "camera_ip": current["camera_ip"], "status": result["status"],
            "status_text": status_label(result["status"]), "message": result["message"]}


def mark_nvr_unreachable_channels(conn, nvr, message):
    channels = conn.execute("select * from channels where nvr_id=? and enabled=1", (nvr["id"],)).fetchall()
    return [item for channel in channels
            if (item := save_channel_result(conn, nvr, channel, error_result(message))) is not None]


def monitor_channels_job(specific_nvr_id=None, blocked=None):
    blocked = blocked if blocked is not None else {}
    checked = []
    nvr_errors = []
    global_lookback_minutes = get_settings()["global_lookback_minutes"]
    for nvr in active_nvrs(specific_nvr_id):
        try:
            if nvr["id"] in blocked:
                raise NvrUnavailable(blocked[nvr["id"]])
            ensure_nvr_reachable(nvr)
            statuses = fetch_channel_statuses(nvr)
            with connect() as conn:
                channels = [dict(row) for row in conn.execute(
                    "select * from channels where nvr_id=? and enabled=1 order by channel, track_id",
                    (nvr["id"],)).fetchall()]
                for channel in channels:
                    status = statuses.get(channel["channel"], {})
                    online = status.get("online")
                    register_camera_ip_change(
                        conn, nvr_id=nvr["id"], channel_id=channel["id"], channel=channel["channel"],
                        track_id=channel["track_id"], old_ip=channel["camera_ip"],
                        new_ip=status.get("ip_address"), source="monitor_status",
                    )
                    conn.execute(
                        """update channels set camera_ip=coalesce(?,camera_ip),
                           channel_online=coalesce(?,channel_online),
                           channel_state=coalesce(?,channel_state) where id=?""",
                        (status.get("ip_address"), None if online is None else int(online),
                         status.get("status"), channel["id"]),
                    )
                    channel["channel_online"] = None if online is None else int(online)
                    channel["channel_state"] = status.get("status")
            for channel in channels:
                if nvr["id"] in blocked:
                    result = error_result(blocked[nvr["id"]])
                elif channel["channel_online"] is None:
                    result = error_result("Estado atual da camera indisponivel; verificar comunicacao com NVR")
                else:
                    try:
                        result = check_channel_recording(nvr, channel, global_lookback_minutes)
                    except NvrUnavailable as exc:
                        blocked[nvr["id"]] = str(exc)[:300]
                        nvr_errors.append({"nvr": nvr["name"], "message": str(exc)[:300]})
                        result = error_result(str(exc))
                with connect() as conn:
                    item = save_channel_result(conn, nvr, channel, result)
                if item:
                    checked.append(item)
        except Exception as exc:
            blocked[nvr["id"]] = str(exc)[:300]
            nvr_errors.append({"nvr": nvr["name"], "message": str(exc)[:300]})
            with connect() as conn:
                checked.extend(mark_nvr_unreachable_channels(conn, nvr, str(exc)))
    return {"summary": {
        "checked": len(checked), "ok": sum(i["status"] == "ok" for i in checked),
        "offline": sum(i["status"] == "offline" for i in checked),
        "no_recording": sum(i["status"] == "no_recording" for i in checked),
        "error": sum(i["status"] == "error" for i in checked), "nvr_errors": len(nvr_errors)},
        "items": checked, "nvr_errors": nvr_errors}


def run_scheduled_cycle(force=False):
    settings = get_settings()
    if not force and not settings["scheduler_enabled"]:
        SCHEDULER_STATE["last_message"] = "agendamento desativado"
        return {"skipped": True}
    if not JOB_LOCK.acquire(blocking=False):
        SCHEDULER_STATE["last_message"] = "execucao anterior ainda em andamento"
        return {"skipped": True}
    run_id = None
    try:
        SCHEDULER_STATE["running"] = True
        SCHEDULER_STATE["last_started_at"] = sao_paulo_iso()
        with connect() as conn:
            cursor = conn.execute(
                "insert into scheduler_runs(started_at, status, message) values(?, ?, ?)",
                (SCHEDULER_STATE["last_started_at"], "running", "execucao em andamento"),
            )
            run_id = cursor.lastrowid
        result = {}
        blocked = {}
        result["discover"] = discover_all_channels_job(source="scheduler_discover", blocked=blocked)["summary"]
        result["monitor"] = monitor_channels_job(blocked=blocked)["summary"]
        result["hdds"] = update_all_hdds_job(blocked=blocked)["summary"]
        failures = sum(part.get("error", 0) + part.get("nvr_errors", 0) for part in result.values())
        successes = sum(part.get("ok", 0) + part.get("offline", 0) + part.get("no_recording", 0) for part in result.values())
        run_status = "partial" if failures and successes else "error" if failures else "ok"
        run_message = {"partial": "execucao concluida com falhas", "error": "execucao falhou", "ok": "execucao concluida"}[run_status]
        SCHEDULER_STATE["last_finished_at"] = sao_paulo_iso()
        SCHEDULER_STATE["last_summary"] = result
        SCHEDULER_STATE["last_message"] = run_message
        with connect() as conn:
            conn.execute(
                """
                update scheduler_runs
                set finished_at=?, status=?, message=?, discover_summary=?, monitor_summary=?
                where id=?
                """,
                (
                    SCHEDULER_STATE["last_finished_at"],
                    run_status,
                    run_message,
                    json.dumps(result.get("discover"), ensure_ascii=False),
                    json.dumps(result.get("monitor"), ensure_ascii=False),
                    run_id,
                ),
            )
        return result
    except Exception as exc:
        SCHEDULER_STATE["last_finished_at"] = sao_paulo_iso()
        SCHEDULER_STATE["last_message"] = str(exc)
        try:
            with connect() as conn:
                conn.execute(
                    """
                    update scheduler_runs
                    set finished_at=?, status=?, message=?
                    where id=?
                    """,
                    (SCHEDULER_STATE["last_finished_at"], "error", str(exc)[:500], run_id),
                )
        except Exception:
            pass
        traceback.print_exc()
        return {"error": str(exc)}
    finally:
        SCHEDULER_STATE["running"] = False
        JOB_LOCK.release()


def run_exclusive_job(callback):
    if not JOB_LOCK.acquire(blocking=False):
        return {"error": "execucao automatica em andamento; tente novamente em instantes"}
    try:
        return callback()
    finally:
        JOB_LOCK.release()


def scheduler_worker():
    SCHEDULER_WAKE.wait(5)
    while True:
        SCHEDULER_WAKE.clear()
        try:
            settings = get_settings()
            if settings["scheduler_enabled"]:
                run_scheduled_cycle()
            interval = max(1, int(get_settings()["scheduler_interval_minutes"])) * 60
        except Exception as exc:
            SCHEDULER_STATE["last_message"] = f"Erro na agenda; nova tentativa em 30s: {exc}"
            traceback.print_exc()
            interval = 30
        SCHEDULER_WAKE.wait(interval)


def start_scheduler():
    thread = threading.Thread(target=scheduler_worker, name="hikvision-scheduler", daemon=True)
    thread.start()


def import_nvrs_txt(text):
    imported = 0
    skipped = 0
    errors = []
    with connect() as conn:
        for line_number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.upper().startswith("NOME;"):
                continue
            parts = [part.strip() for part in line.split(";")]
            if len(parts) < 4:
                skipped += 1
                errors.append(f"Linha {line_number}: esperado NOME;IP;USER;SENHA")
                continue
            name, ip, username, password = parts[:4]
            try:
                base_url = normalize_base_url(ip)
                timestamp = now_iso()
                conn.execute(
                    """
                    insert into nvrs(name, base_url, username, password, password_encrypted, lookback_minutes, created_at, updated_at)
                    values(?, ?, ?, ?, 1, 0, ?, ?)
                    on conflict(base_url) do update set
                        name=excluded.name,
                        username=excluded.username,
                        password=excluded.password,
                        password_encrypted=1,
                        updated_at=excluded.updated_at
                    """,
                    (name, base_url, username, CREDENTIALS.encrypt(password), timestamp, timestamp),
                )
                imported += 1
            except Exception as exc:
                skipped += 1
                errors.append(f"Linha {line_number}: {exc}")
    return {"imported": imported, "skipped": skipped, "errors": errors}


def list_active_alerts(conn):
    status_rows = conn.execute(
        """
        select 'channel_status' as alert_type, c.id as ref_id,
               n.id as nvr_id, n.name as nvr_name, n.base_url, c.channel, c.track_id, c.camera_ip,
               c.last_status, c.last_message, c.last_checked_at,
               c.last_status_changed_at, c.last_recording_start, c.last_recording_end,
               null as old_ip, null as new_ip, null as change_type, null as detected_at
        from channels c
        join nvrs n on n.id = c.nvr_id
        left join alert_acknowledgements a
          on a.alert_type = 'channel_status'
         and a.ref_id = c.id
         and a.status = c.last_status
         and coalesce(a.status_changed_at, '') = coalesce(c.last_status_changed_at, '')
        where c.enabled=1 and n.enabled=1 and c.last_status in ('no_recording', 'error')
          and a.id is null
        order by n.name, c.channel, c.track_id
        """
    ).fetchall()
    change_rows = conn.execute(
        """
        select 'ip_change' as alert_type, h.id as ref_id,
               n.id as nvr_id, n.name as nvr_name, n.base_url, h.channel, h.track_id,
               h.new_ip as camera_ip,
               'change' as last_status,
               case
                 when h.change_type = 'new_ip' then 'novo IP de camera descoberto'
                 when h.change_type = 'ip_removed' then 'camera removida do canal'
                 else 'IP da camera substituido'
               end as last_message,
               h.detected_at as last_checked_at,
               h.detected_at as last_status_changed_at,
               null as last_recording_start, null as last_recording_end,
               h.old_ip, h.new_ip, h.change_type, h.detected_at
        from camera_ip_changes h
        join nvrs n on n.id = h.nvr_id
        left join alert_acknowledgements a
          on a.alert_type = 'ip_change'
         and a.ref_id = h.id
        where n.enabled=1 and a.id is null
        order by h.detected_at desc, h.id desc
        """
    ).fetchall()
    hdd_rows = conn.execute(
        """
        select 'hdd_status' as alert_type, h.id as ref_id,
               n.id as nvr_id, n.name as nvr_name, n.base_url,
               'HDD ' || h.hdd_id as channel, null as track_id, null as camera_ip,
               'hdd_error' as last_status, h.status as alert_status,
               case when lower(trim(h.status))='missing'
                    then 'HDD ' || h.hdd_id || ' nao encontrado na ultima consulta'
                    else 'HDD ' || h.hdd_id ||
                         case when h.name is not null and h.name != '' then ' (' || h.name || ')' else '' end ||
                         ': estado ' || h.status end as last_message,
               h.last_checked_at, h.last_status_changed_at,
               null as last_recording_start, null as last_recording_end,
               null as old_ip, null as new_ip, null as change_type, null as detected_at
        from hdds h
        join nvrs n on n.id=h.nvr_id
        left join alert_acknowledgements a
          on a.alert_type='hdd_status' and a.ref_id=h.id
         and a.status=h.status
         and coalesce(a.status_changed_at, '')=coalesce(h.last_status_changed_at, '')
        where n.enabled=1 and h.status is not null and trim(h.status) != ''
          and lower(trim(h.status)) not in ('ok', 'normal', 'healthy', 'rw', 'readwrite', 'working', 'idle', 'notexist')
          and a.id is null
        order by n.name, h.hdd_id
        """
    ).fetchall()
    items = [row_to_dict(row) for row in status_rows] + [row_to_dict(row) for row in change_rows] + [row_to_dict(row) for row in hdd_rows]
    items.sort(key=lambda item: item.get("last_checked_at") or "", reverse=True)
    return items


class App(BaseHTTPRequestHandler):
    server_version = "HikvisionManagerApp/0.1"

    def setup(self):
        # TLS negotiation runs in this request worker, never in the accept loop.
        self.request.settimeout(15)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def do_GET(self):
        try:
            if not require_auth(self):
                return
            request_path = urllib.parse.urlparse(self.path).path
            if request_path == "/api/health":
                return json_response(self, 200, {"ok": True, "db": str(DB_PATH)})
            if request_path == "/api/me":
                return json_response(self, 200, {"user": get_session_user(self)})
            if request_path == "/api/users":
                if not require_admin(self):
                    return
                return self.get_users()
            if request_path == "/api/nvrs":
                return self.get_nvrs()
            if request_path == "/api/channels/tree":
                return self.get_channel_tree()
            if request_path == "/api/locations":
                with connect() as conn:
                    user = get_session_user(self)
                    items = [dict(r) for r in conn.execute(
                        """select l.*, count(n.id) as nvrs from locations l
                           left join nvrs n on n.location_id=l.id
                           where ?='admin' or exists (
                             select 1 from app_user_cities uc where uc.user_id=? and uc.city=l.city
                           )
                           group by l.id order by l.city, l.block""",
                        (user["role"], user["id"]))]
                return json_response(self, 200, {"items": items})
            if re.fullmatch(r"/api/nvrs/\d+/channels/\d+/snapshot", request_path):
                if not require_admin(self):
                    return
                return self.get_channel_snapshot()
            if request_path.startswith("/api/nvrs/") and request_path.endswith("/channels"):
                return self.get_channels()
            if request_path.startswith("/api/nvrs/") and request_path.endswith("/hdds"):
                return self.get_hdds()
            if request_path == "/api/hdds":
                return self.get_hdds()
            if request_path == "/api/dashboard":
                return self.get_dashboard()
            if request_path == "/api/alerts":
                return self.get_alerts()
            if request_path == "/api/alerts/acknowledged":
                return self.get_acknowledged_alerts()
            if request_path == "/api/camera-ip-changes":
                return self.get_camera_ip_changes()
            if request_path == "/api/settings":
                if not require_admin(self):
                    return
                return json_response(self, 200, get_settings())
            if request_path == "/api/scheduler/history":
                if not require_admin(self):
                    return
                return self.get_scheduler_history()
            if request_path.startswith("/api/"):
                return json_response(self, 404, {"error": "rota nao encontrada"})
            return self.serve_static()
        except Exception as exc:
            return json_response(self, 500, {"error": str(exc)})

    def do_POST(self):
        try:
            if not require_auth(self):
                return
            if self.path == "/api/login":
                return self.login()
            if self.path == "/api/logout":
                return self.logout()
            if self.path == "/api/change-password":
                return self.change_password()
            if not require_admin(self):
                return
            if self.path == "/api/users":
                return self.create_user()
            if re.fullmatch(r"/api/users/\d+/cities", self.path):
                return self.update_user_cities()
            if re.fullmatch(r"/api/users/\d+/password", self.path):
                return self.reset_user_password()
            if self.path == "/api/nvrs":
                return self.create_nvr()
            if self.path == "/api/locations":
                return self.save_location()
            if self.path.startswith("/api/nvrs/") and self.path.endswith("/update"):
                return self.update_nvr()
            if self.path == "/api/import/nvrs-txt":
                return self.import_nvrs()
            if self.path.startswith("/api/nvrs/") and self.path.endswith("/discover"):
                return json_response(self, 200, run_exclusive_job(lambda: self.discover_channels_payload()))
            if self.path == "/api/discover/run":
                return json_response(self, 200, run_exclusive_job(lambda: discover_all_channels_job(source="manual_discover")))
            if self.path == "/api/verify-now/run":
                return json_response(self, 200, run_exclusive_job(lambda: verify_now_job()))
            if self.path.startswith("/api/nvrs/") and self.path.endswith("/verify-now"):
                nvr_id = int(self.path.split("/")[3])
                return json_response(self, 200, run_exclusive_job(lambda: verify_now_job(nvr_id)))
            if self.path == "/api/monitor/run":
                return json_response(self, 200, run_exclusive_job(lambda: monitor_channels_job()))
            if self.path.startswith("/api/nvrs/") and self.path.endswith("/monitor"):
                nvr_id = int(self.path.split("/")[3])
                return json_response(self, 200, run_exclusive_job(lambda: monitor_channels_job(nvr_id)))
            if self.path == "/api/hdds/run":
                return json_response(self, 200, run_exclusive_job(lambda: update_all_hdds_job()))
            if self.path.startswith("/api/nvrs/") and self.path.endswith("/hdds/run"):
                nvr_id = int(self.path.split("/")[3])
                return json_response(self, 200, run_exclusive_job(lambda: update_all_hdds_job(nvr_id)))
            if self.path == "/api/settings":
                return self.update_settings()
            if self.path == "/api/alerts/ack":
                return self.ack_alert()
            if self.path == "/api/scheduler/run":
                return json_response(self, 200, run_scheduled_cycle(force=True))
            return json_response(self, 404, {"error": "rota nao encontrada"})
        except Exception as exc:
            return json_response(self, 500, {"error": str(exc)})

    def do_DELETE(self):
        try:
            if not require_auth(self):
                return
            if not require_admin(self):
                return
            request_path = urllib.parse.urlparse(self.path).path
            if re.fullmatch(r"/api/users/\d+", request_path):
                return self.delete_user()
            if re.fullmatch(r"/api/locations/\d+", request_path):
                with connect() as conn:
                    location_id = int(request_path.rsplit("/", 1)[1])
                    location = conn.execute("select city from locations where id=?", (location_id,)).fetchone()
                    if not location:
                        return json_response(self, 404, {"error": "Localizacao nao encontrada."})
                    if conn.execute("select 1 from nvrs where location_id=?", (location_id,)).fetchone():
                        return json_response(self, 409, {"error": "Transfira os NVRs antes de excluir esta localizacao."})
                    conn.execute("delete from locations where id=?", (location_id,))
                    revoke_missing_city(conn, location["city"])
                return json_response(self, 200, {"deleted": True})
            if request_path == "/api/alerts/acknowledged":
                return self.delete_all_acknowledgements()
            if re.fullmatch(r"/api/alerts/acknowledged/\d+", request_path):
                return self.delete_acknowledgement()
            if request_path.startswith("/api/nvrs/"):
                return self.delete_nvr()
            return json_response(self, 404, {"error": "rota nao encontrada"})
        except Exception as exc:
            return json_response(self, 500, {"error": str(exc)})

    def get_nvrs(self):
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            rows = conn.execute(
                """
                select n.id, n.name, n.base_url, n.username, n.lookback_minutes,
                       n.location_id, l.city, l.block,
                       n.nvr_time_offset_hours, n.enabled, n.created_at, n.updated_at,
                       count(c.id) as channel_count,
                       sum(case when c.last_status = 'ok' then 1 else 0 end) as ok_channels,
                       sum(case when c.last_status = 'offline' then 1 else 0 end) as offline_channels,
                       sum(case when c.last_status = 'no_recording' then 1 else 0 end) as no_recording_channels,
                       sum(case when c.last_status = 'error' then 1 else 0 end) as error_channels,
                       sum(case when c.last_status in ('offline', 'no_recording', 'error') then 1 else 0 end) as alert_channels,
                       (
                         select count(*)
                         from camera_ip_changes h
                         left join alert_acknowledgements a
                           on a.alert_type = 'ip_change'
                          and a.ref_id = h.id
                         where h.nvr_id = n.id
                           and a.id is null
                       ) as change_alerts,
                       0 as status_change_alerts
                from nvrs n
                left join locations l on l.id=n.location_id
                left join channels c on c.nvr_id = n.id and c.enabled=1
                where n.enabled=1
                group by n.id
                order by n.name
                """
            ).fetchall()
            items = [row_to_dict(row) for row in rows if allowed is None or row["id"] in allowed]
            alerts = [alert for alert in list_active_alerts(conn) if allowed is None or alert["nvr_id"] in allowed]
            for item in items:
                pending = [alert for alert in alerts if alert["nvr_id"] == item["id"]]
                item["pending_alerts"] = len(pending)
                for status in ("offline", "no_recording", "error"):
                    item["pending_" + status] = sum(alert["last_status"] == status for alert in pending)
                item["pending_hdd_error"] = sum(alert["alert_type"] == "hdd_status" for alert in pending)
                item["alert_channels"] = sum(alert["alert_type"] == "channel_status" for alert in pending)
        return json_response(self, 200, {"items": items})

    def login(self):
        data = read_json(self)
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        with connect() as conn:
            user = conn.execute("select * from app_users where username=?", (username,)).fetchone()
        if not user:
            return json_response(self, 401, {"error": "usuario ou senha invalido"})
        password_hash, _ = hash_password(password, user["salt"])
        if not hmac.compare_digest(password_hash, user["password_hash"]):
            return json_response(self, 401, {"error": "usuario ou senha invalido"})
        token = create_session(user["id"])
        body = json.dumps({
            "ok": True,
            "user": {
                "username": user["username"],
                "role": user["role"],
                "must_change_password": bool(user["must_change_password"]),
            },
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        secure = "; Secure" if isinstance(self.connection, ssl.SSLSocket) else ""
        self.send_header("Set-Cookie", f"hikvision_session={token}; Path=/; HttpOnly; SameSite=Lax{secure}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_users(self):
        with connect() as conn:
            rows = conn.execute(
                "select id, username, role, must_change_password, created_at from app_users where role='viewer' order by username"
            ).fetchall()
            cities = conn.execute("select user_id, city from app_user_cities order by city").fetchall()
        items = [row_to_dict(row) for row in rows]
        for item in items:
            item["cities"] = [row["city"] for row in cities if row["user_id"] == item["id"]]
        return json_response(self, 200, {"items": items})

    def create_user(self):
        data = read_json(self)
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if data.get("role", "viewer") != "viewer":
            return json_response(self, 400, {"error": "apenas usuarios de visualizacao podem ser cadastrados"})
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username):
            return json_response(self, 400, {"error": "usuario deve ter 3 a 64 caracteres: letras, numeros, ponto, traco ou sublinhado"})
        if not 8 <= len(password) <= 128:
            return json_response(self, 400, {"error": "senha deve ter entre 8 e 128 caracteres"})
        with connect() as conn:
            try:
                cities = validated_user_cities(conn, data.get("cities"))
            except ValueError as exc:
                return json_response(self, 400, {"error": str(exc)})
            if conn.execute("select 1 from app_users where lower(username)=lower(?)", (username,)).fetchone():
                return json_response(self, 409, {"error": "usuario ja cadastrado"})
            password_hash, salt = hash_password(password)
            cursor = conn.execute(
                "insert into app_users(username,password_hash,salt,role,must_change_password,created_at) values(?,?,?,'viewer',1,?)",
                (username, password_hash, salt, sao_paulo_iso()),
            )
            conn.executemany("insert into app_user_cities(user_id,city) values(?,?)",
                             [(cursor.lastrowid, city) for city in cities])
        return json_response(self, 201, {"id": cursor.lastrowid, "username": username, "role": "viewer", "cities": cities})

    def update_user_cities(self):
        user_id = int(self.path.split("/")[3])
        with connect() as conn:
            if not conn.execute("select 1 from app_users where id=? and role='viewer'", (user_id,)).fetchone():
                return json_response(self, 404, {"error": "usuario de visualizacao nao encontrado"})
            try:
                cities = validated_user_cities(conn, read_json(self).get("cities"))
            except ValueError as exc:
                return json_response(self, 400, {"error": str(exc)})
            conn.execute("delete from app_user_cities where user_id=?", (user_id,))
            conn.executemany("insert into app_user_cities(user_id,city) values(?,?)",
                             [(user_id, city) for city in cities])
        return json_response(self, 200, {"id": user_id, "cities": cities})

    def reset_user_password(self):
        user_id = int(self.path.split("/")[3])
        password = (read_json(self).get("password") or "")
        if not 8 <= len(password) <= 128:
            return json_response(self, 400, {"error": "senha deve ter entre 8 e 128 caracteres"})
        with connect() as conn:
            if not conn.execute("select 1 from app_users where id=? and role='viewer'", (user_id,)).fetchone():
                return json_response(self, 404, {"error": "usuario de visualizacao nao encontrado"})
            password_hash, salt = hash_password(password)
            conn.execute("update app_users set password_hash=?,salt=?,must_change_password=1 where id=?", (password_hash, salt, user_id))
            conn.execute("delete from app_sessions where user_id=?", (user_id,))
        return json_response(self, 200, {"ok": True})

    def delete_user(self):
        user_id = int(urllib.parse.urlparse(self.path).path.rsplit("/", 1)[1])
        with connect() as conn:
            deleted = conn.execute("delete from app_users where id=? and role='viewer'", (user_id,)).rowcount
        if not deleted:
            return json_response(self, 404, {"error": "usuario de visualizacao nao encontrado"})
        return json_response(self, 200, {"deleted": True})

    def logout(self):
        clear_session(self)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", "hikvision_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def change_password(self):
        try:
            change_password(self, read_json(self))
            return json_response(self, 200, {"ok": True})
        except Exception as exc:
            return json_response(self, 400, {"error": str(exc)})

    def get_channel_tree(self):
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            rows = conn.execute(
                """select c.id, c.nvr_id, c.channel, c.track_id, c.camera_ip, c.last_status
                   from channels c join nvrs n on n.id=c.nvr_id
                   where c.enabled=1 and n.enabled=1
                   order by n.name, c.channel, c.track_id"""
            ).fetchall()
        return json_response(self, 200, {"items": [row_to_dict(row) for row in rows if allowed is None or row["nvr_id"] in allowed]})

    def get_channels(self):
        nvr_id = int(urllib.parse.urlparse(self.path).path.split("/")[3])
        with connect() as conn:
            if not can_view_nvr(conn, get_session_user(self), nvr_id):
                return json_response(self, 404, {"error": "NVR nao encontrado"})
            rows = conn.execute(
                "select * from channels where nvr_id=? and enabled=1 order by channel, track_id",
                (nvr_id,),
            ).fetchall()
        return json_response(self, 200, {"items": [row_to_dict(row) for row in rows]})

    def get_channel_snapshot(self):
        parts = urllib.parse.urlparse(self.path).path.split("/")
        nvr_id = int(parts[3])
        channel_id = int(parts[5])
        with connect() as conn:
            nvr = conn.execute("select * from nvrs where id=? and enabled=1", (nvr_id,)).fetchone()
            channel = conn.execute(
                "select * from channels where id=? and nvr_id=? and enabled=1",
                (channel_id, nvr_id),
            ).fetchone()
        if not nvr or not channel:
            return json_response(self, 404, {"error": "NVR ou canal nao encontrado"})
        try:
            body, content_type = fetch_channel_snapshot(nvr, channel)
            return binary_response(self, 200, body, content_type)
        except Exception as exc:
            return json_response(self, 502, {"error": str(exc)})

    def get_hdds(self):
        specific_nvr_id = None
        if self.path.startswith("/api/nvrs/"):
            specific_nvr_id = int(self.path.split("/")[3])
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            if specific_nvr_id and allowed is not None and specific_nvr_id not in allowed:
                return json_response(self, 404, {"error": "NVR nao encontrado"})
            if specific_nvr_id:
                rows = conn.execute(
                    """
                    select h.*, n.name as nvr_name, n.base_url
                    from hdds h
                    join nvrs n on n.id = h.nvr_id
                    where h.nvr_id=?
                    order by h.hdd_id
                    """,
                    (specific_nvr_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    select h.*, n.name as nvr_name, n.base_url
                    from hdds h
                    join nvrs n on n.id = h.nvr_id
                    order by n.name, h.hdd_id
                    """
                ).fetchall()
        return json_response(self, 200, {"items": [row_to_dict(row) for row in rows if allowed is None or row["nvr_id"] in allowed]})

    def get_dashboard(self):
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            summary = conn.execute(
                """
                select
                    count(*) as nvrs,
                    (select count(*) from channels c join nvrs n on n.id=c.nvr_id where c.enabled=1 and n.enabled=1) as channels,
                    (select count(*) from channels c join nvrs n on n.id=c.nvr_id where c.enabled=1 and n.enabled=1 and last_status in ('offline', 'no_recording', 'error')) as alerts,
                    (select count(*) from channels c join nvrs n on n.id=c.nvr_id where c.enabled=1 and n.enabled=1 and last_status = 'ok') as ok_channels,
                    (select count(*) from channels c join nvrs n on n.id=c.nvr_id where c.enabled=1 and n.enabled=1 and last_status = 'offline') as offline_channels,
                    (select count(*) from channels c join nvrs n on n.id=c.nvr_id where c.enabled=1 and n.enabled=1 and last_status = 'no_recording') as no_recording_channels,
                    (select count(*) from channels c join nvrs n on n.id=c.nvr_id where c.enabled=1 and n.enabled=1 and last_status = 'error') as error_channels
                from nvrs where enabled=1
                """
            ).fetchone()
            nvrs = conn.execute(
                """
                select n.id, n.name, n.base_url, count(c.id) as channels,
                       sum(case when c.last_status = 'ok' then 1 else 0 end) as ok_channels,
                       sum(case when c.last_status = 'offline' then 1 else 0 end) as offline_channels,
                       sum(case when c.last_status = 'no_recording' then 1 else 0 end) as no_recording_channels,
                       sum(case when c.last_status = 'error' then 1 else 0 end) as error_channels
                from nvrs n
                left join channels c on c.nvr_id = n.id and c.enabled=1
                where n.enabled=1
                group by n.id
                order by n.name
                """
            ).fetchall()
            nvrs = [row_to_dict(row) for row in nvrs if allowed is None or row["id"] in allowed]
            summary = row_to_dict(summary) if allowed is None else {
                "nvrs": len(nvrs),
                "channels": sum(row["channels"] or 0 for row in nvrs),
                "ok_channels": sum(row["ok_channels"] or 0 for row in nvrs),
                "offline_channels": sum(row["offline_channels"] or 0 for row in nvrs),
                "no_recording_channels": sum(row["no_recording_channels"] or 0 for row in nvrs),
                "error_channels": sum(row["error_channels"] or 0 for row in nvrs),
            }
            active_alerts = [item for item in list_active_alerts(conn) if allowed is None or item["nvr_id"] in allowed]
            summary["alerts"] = len(active_alerts)
            summary["hdd_errors"] = sum(item["alert_type"] == "hdd_status" for item in active_alerts)
        return json_response(
            self,
            200,
            {
                "summary": summary,
                "nvrs": nvrs,
            },
        )

    def get_alerts(self):
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            items = [item for item in list_active_alerts(conn) if allowed is None or item["nvr_id"] in allowed]
        return json_response(self, 200, {"items": items})

    def get_acknowledged_alerts(self):
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            if allowed is not None and not allowed:
                return json_response(self, 200, {"items": []})
            scope = "" if allowed is None else f" and n.id in ({','.join('?' for _ in allowed)})"
            rows = conn.execute(
                f"""
                select a.id, a.alert_type, a.ref_id, a.status, a.status_changed_at,
                       a.acknowledged_at,
                       n.id as nvr_id, n.name as nvr_name,
                       coalesce(cast(c.channel as text), cast(h.channel as text), cast(sc.channel as text), 'HDD ' || d.hdd_id) as channel,
                       coalesce(c.track_id, h.track_id, sc.track_id) as track_id,
                       coalesce(c.camera_ip, h.new_ip) as camera_ip,
                       coalesce(h.old_ip, sc.old_status) as old_ip,
                       coalesce(h.new_ip, sc.new_status) as new_ip,
                       case when a.alert_type='hdd_status' then 'hdd_status'
                            else coalesce(h.change_type, 'status_change') end as change_type,
                       d.status as hdd_status
                from alert_acknowledgements a
                left join channels c
                  on a.alert_type = 'channel_status'
                 and a.ref_id = c.id
                left join camera_ip_changes h
                  on a.alert_type = 'ip_change'
                 and a.ref_id = h.id
                left join channel_status_changes sc
                  on a.alert_type = 'status_change'
                 and a.ref_id = sc.id
                left join hdds d
                  on a.alert_type = 'hdd_status'
                 and a.ref_id = d.id
                left join nvrs n
                  on n.id = coalesce(c.nvr_id, h.nvr_id, sc.nvr_id, d.nvr_id)
                where a.hidden_at is null {scope}
                order by a.acknowledged_at desc, a.id desc
                limit 500
                """, tuple(allowed or ())
            ).fetchall()
        return json_response(self, 200, {"items": [row_to_dict(row) for row in rows]})

    def get_camera_ip_changes(self):
        with connect() as conn:
            allowed = visible_nvr_ids(conn, get_session_user(self))
            if allowed is not None and not allowed:
                return json_response(self, 200, {"items": []})
            scope = "" if allowed is None else f" where n.id in ({','.join('?' for _ in allowed)})"
            rows = conn.execute(
                f"""
                select h.id, n.id as nvr_id, n.name as nvr_name, n.base_url, h.channel, h.track_id,
                       h.old_ip, h.new_ip, h.change_type, h.detected_at, h.source
                from camera_ip_changes h
                join nvrs n on n.id = h.nvr_id
                {scope}
                order by h.detected_at desc, h.id desc
                limit 300
                """, tuple(allowed or ())
            ).fetchall()
        return json_response(self, 200, {"items": [row_to_dict(row) for row in rows]})

    def get_scheduler_history(self):
        with connect() as conn:
            rows = conn.execute(
                """
                select id, started_at, finished_at, status, message,
                       discover_summary, monitor_summary
                from scheduler_runs
                order by id desc
                limit 200
                """
            ).fetchall()
        items = []
        for row in rows:
            item = row_to_dict(row)
            for key in ("discover_summary", "monitor_summary"):
                try:
                    item[key] = json.loads(item[key]) if item[key] else None
                except Exception:
                    item[key] = None
            items.append(item)
        return json_response(self, 200, {"items": items})

    def save_location(self):
        data = read_json(self)
        city, block = str(data.get("city", "")).strip(), str(data.get("block", "")).strip()
        if not city or not block or len(city) > 120 or len(block) > 120:
            return json_response(self, 400, {"error": "Informe cidade e bloco (ate 120 caracteres)."})
        with connect() as conn:
            try:
                if data.get("id"):
                    old = conn.execute("select city from locations where id=?", (int(data["id"]),)).fetchone()
                    if not old:
                        return json_response(self, 404, {"error": "Localizacao nao encontrada."})
                    cursor = conn.execute("update locations set city=?, block=? where id=?", (city, block, int(data["id"])))
                    if not cursor.rowcount:
                        return json_response(self, 404, {"error": "Localizacao nao encontrada."})
                    revoke_missing_city(conn, old["city"])
                else:
                    conn.execute("insert into locations(city, block) values(?, ?)", (city, block))
            except sqlite3.IntegrityError:
                return json_response(self, 409, {"error": "Cidade e bloco ja cadastrados."})
        return json_response(self, 200, {"saved": True})

    def create_nvr(self):
        data = read_json(self)
        timestamp = now_iso()
        base_url = normalize_base_url(data.get("base_url") or data.get("ip"))
        with connect() as conn:
            cursor = conn.execute(
                """
                insert into nvrs(name, base_url, username, password, lookback_minutes, nvr_time_offset_hours, created_at, updated_at, password_encrypted)
                values(?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    data.get("name", "").strip(),
                    base_url,
                    data.get("username", "").strip(),
                    CREDENTIALS.encrypt(data.get("password", "")),
                    validate_lookback(data.get("lookback_minutes")),
                    int(data.get("nvr_time_offset_hours") if data.get("nvr_time_offset_hours") is not None else -3),
                    timestamp,
                    timestamp,
                ),
            )
            nvr_id = cursor.lastrowid
            conn.execute("update nvrs set location_id=? where id=?", (data.get("location_id") or None, nvr_id))
        return json_response(self, 201, {"id": nvr_id})

    def update_nvr(self):
        nvr_id = int(self.path.split("/")[3])
        data = read_json(self)
        timestamp = now_iso()
        base_url = normalize_base_url(data.get("base_url") or data.get("ip"))
        with connect() as conn:
            existing = conn.execute("select * from nvrs where id=?", (nvr_id,)).fetchone()
            if not existing:
                return json_response(self, 404, {"error": "NVR nao encontrado"})
            if "location_id" in data:
                conn.execute("update nvrs set location_id=? where id=?", (data.get("location_id") or None, nvr_id))
            password = data.get("password")
            if password is None or password == "":
                password = existing["password"]
            else:
                password = CREDENTIALS.encrypt(password)
            conn.execute(
                """
                update nvrs
                set name=?, base_url=?, username=?, password=?, password_encrypted=1,
                    lookback_minutes=?, nvr_time_offset_hours=?, updated_at=?
                where id=?
                """,
                (
                    data.get("name", "").strip(),
                    base_url,
                    data.get("username", "").strip(),
                    password,
                    validate_lookback(data.get("lookback_minutes", existing["lookback_minutes"])),
                    int(data.get("nvr_time_offset_hours") if data.get("nvr_time_offset_hours") is not None else -3),
                    timestamp,
                    nvr_id,
                ),
            )
        return json_response(self, 200, {"id": nvr_id, "updated": True})

    def import_nvrs(self):
        data = read_json(self)
        if "text" in data:
            text = data["text"]
        elif "path" in data:
            path = Path(data["path"])
            text = path.read_text(encoding="utf-8-sig")
        else:
            return json_response(self, 400, {"error": "envie text ou path"})
        return json_response(self, 200, import_nvrs_txt(text))

    def update_settings(self):
        data = read_json(self)
        return json_response(self, 200, save_settings(data))

    def ack_alert(self):
        acknowledge_alert(read_json(self))
        return json_response(self, 200, {"acknowledged": True})

    def delete_acknowledgement(self):
        ack_id = int(urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1])
        with connect() as conn:
            cursor = conn.execute(
                "update alert_acknowledgements set hidden_at=? where id=? and hidden_at is null",
                (now_iso(), ack_id),
            )
        return json_response(self, 200, {"hidden": cursor.rowcount > 0})

    def delete_all_acknowledgements(self):
        with connect() as conn:
            cursor = conn.execute(
                "update alert_acknowledgements set hidden_at=? where hidden_at is null",
                (now_iso(),),
            )
        return json_response(self, 200, {"hidden": cursor.rowcount})

    def discover_channels(self):
        return json_response(self, 200, self.discover_channels_payload())

    def discover_channels_payload(self):
        nvr_id = int(self.path.split("/")[3])
        return discover_all_channels_job(nvr_id, source="manual_discover")

    def discover_all_channels(self):
        return json_response(self, 200, discover_all_channels_job(source="manual_discover"))

    def run_monitor(self):
        specific_nvr_id = None
        if self.path.startswith("/api/nvrs/"):
            specific_nvr_id = int(self.path.split("/")[3])
        return json_response(self, 200, monitor_channels_job(specific_nvr_id))

    def delete_nvr(self):
        nvr_id = int(self.path.split("/")[3])
        with connect() as conn:
            conn.execute("delete from nvrs where id=?", (nvr_id,))
        return json_response(self, 200, {"deleted": True})

    def serve_static(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            path = "/index.html"
        safe = re.sub(r"[^a-zA-Z0-9._/-]", "", path).lstrip("/")
        target = (FRONTEND / safe).resolve()
        if not target.is_relative_to(FRONTEND.resolve()) or not target.is_file():
            return text_response(self, 404, "arquivo nao encontrado")
        content_type = "text/html; charset=utf-8"
        if target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_http_server(settings):
    context = None
    port = PORT
    protocol = "http"
    if settings["https_enabled"]:
        cert_path = Path(settings["https_cert_path"])
        key_path = Path(settings["https_key_path"])
        if not cert_path.is_absolute():
            cert_path = ROOT / cert_path
        if not key_path.is_absolute():
            key_path = ROOT / key_path
        if not cert_path.is_file() or not key_path.is_file():
            raise RuntimeError("HTTPS habilitado: certificado ou chave ausente. HTTP nao sera iniciado.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        port = settings["https_port"]
        protocol = "https"
    server = ThreadingHTTPServer((HOST, port), App)
    if context:
        try:
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        except Exception:
            server.server_close()
            raise
    return server, protocol, port


if __name__ == "__main__":
    init_db()
    server, protocol, port = create_http_server(get_settings())
    start_scheduler()
    print(f"Hikvision Manager App em {protocol}://localhost:{port}")
    print(f"Banco: {DB_PATH}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
