from __future__ import annotations

import json
import math
import os
import re
import secrets
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SECRET_DIR = BASE_DIR / ".secrets"
DEFAULT_TESLA_CLIENT_SECRET_FILE = DEFAULT_SECRET_DIR / "tesla_client_secret"
DEFAULT_TESLA_TOKEN_FILE = DEFAULT_SECRET_DIR / "tesla_tokens.json"
DEFAULT_TESLA_CACHE_FILE = BASE_DIR / "tesla_energy_cache.json"
DEFAULT_TESLA_SYNC_STATE_FILE = BASE_DIR / "tesla_energy_sync_state.json"
DEFAULT_TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
DEFAULT_TESLA_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
DEFAULT_TESLA_AUDIENCE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
DEFAULT_TESLA_API_BASE_URL = DEFAULT_TESLA_AUDIENCE
DEFAULT_TESLA_SCOPE = "openid offline_access energy_device_data"
DEFAULT_TESLA_LIVE_TABLE = "tesla_energy_live_samples"
DEFAULT_TESLA_HISTORY_TABLE = "tesla_energy_history"
DEFAULT_TESLA_PARTNER_PUBLIC_KEY_FILE = "/var/www/tesla/.well-known/appspecific/com.tesla.3p.public-key.pem"


def read_secret_file(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_secret_file(path: str | Path, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value.strip() + "\n", encoding="utf-8")
    os.chmod(target, 0o600)


def load_json_file(path: str | Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return default


def save_json_file(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    os.chmod(target, 0o600)


def secret_is_configured(path: str | Path | None) -> bool:
    return bool(read_secret_file(path))


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sql_datetime(value: datetime | None) -> str:
    if value is None:
        return "NULL"
    utc_value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return f"'{utc_value.strftime('%Y-%m-%d %H:%M:%S')}'"


def tesla_history_datetime(value: date, end_of_day: bool = False) -> str:
    clock = datetime.max.time() if end_of_day else datetime.min.time()
    return datetime.combine(value, clock, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def load_tesla_config(raw_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(raw_cfg or {})
    cfg.setdefault("enabled", False)
    cfg.setdefault("client_id", "")
    cfg.setdefault("client_secret_file", str(DEFAULT_TESLA_CLIENT_SECRET_FILE))
    cfg.setdefault("token_file", str(DEFAULT_TESLA_TOKEN_FILE))
    cfg.setdefault("cache_file", str(DEFAULT_TESLA_CACHE_FILE))
    cfg.setdefault("sync_state_file", str(DEFAULT_TESLA_SYNC_STATE_FILE))
    cfg.setdefault("redirect_uri", "http://localhost:5000/callback")
    cfg.setdefault("auth_url", DEFAULT_TESLA_AUTH_URL)
    cfg.setdefault("token_url", DEFAULT_TESLA_TOKEN_URL)
    cfg.setdefault("audience", DEFAULT_TESLA_AUDIENCE)
    cfg.setdefault("api_base_url", str(cfg.get("audience") or DEFAULT_TESLA_API_BASE_URL))
    cfg.setdefault("scope", DEFAULT_TESLA_SCOPE)
    cfg.setdefault("energy_site_id", "")
    cfg.setdefault("poll_seconds", 60)
    cfg.setdefault("history_hours", 24)
    cfg.setdefault("history_max_points", 288)
    cfg.setdefault("live_table", DEFAULT_TESLA_LIVE_TABLE)
    cfg.setdefault("history_table", DEFAULT_TESLA_HISTORY_TABLE)
    cfg.setdefault("partner_domain", "")
    cfg.setdefault("partner_public_key_file", DEFAULT_TESLA_PARTNER_PUBLIC_KEY_FILE)
    cfg.setdefault("public_bind_host", "0.0.0.0")
    cfg.setdefault("public_http_port", 8078)
    cfg.setdefault("public_https_port", 9443)
    cfg.setdefault("public_static_root", "/var/www/tesla")
    cfg.setdefault("public_proxy_target", "http://127.0.0.1:8788")
    cfg.setdefault("tls_cert_file", str(DEFAULT_SECRET_DIR / "tls" / str(cfg.get("partner_domain", "")) / "fullchain.pem"))
    cfg.setdefault("tls_key_file", str(DEFAULT_SECRET_DIR / "tls" / str(cfg.get("partner_domain", "")) / "privkey.pem"))
    cfg.setdefault("monthly_request_budget", 5000)
    cfg.setdefault("monthly_request_reserve", 200)
    cfg.setdefault("history_backfill_days", 90)
    cfg.setdefault("site_info_refresh_hours", 24)
    cfg.setdefault("history_refresh_hours", 24)
    cfg.setdefault("sync_idle_sleep_seconds", 60)
    cfg.setdefault("sync_active_start_hour", 0)   # 0 = no restriction (poll 24h)
    cfg.setdefault("sync_active_end_hour", 24)    # 24 = no restriction
    cfg.setdefault("temporary_live_poll_interval_seconds", None)
    cfg.setdefault("temporary_allow_request_budget_overrun", False)
    return cfg


def sanitized_tesla_config(raw_cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = load_tesla_config(raw_cfg)
    cfg["client_secret_configured"] = secret_is_configured(cfg.get("client_secret_file"))
    cfg.pop("client_secret", None)
    return cfg


class TeslaEnergyStore:
    def __init__(self, database_cfg: dict[str, Any] | None, tesla_cfg: dict[str, Any]):
        db_cfg = dict(database_cfg or {})
        self.enabled = bool(db_cfg.get("enabled", False))
        self.bootstrap = bool(db_cfg.get("bootstrap", True))
        self.socket = str(db_cfg.get("socket", "/run/mysqld/mysqld.sock")).strip()
        self.host = str(db_cfg.get("host", "127.0.0.1")).strip()
        self.port = int(db_cfg.get("port", 3306))
        self.user = str(db_cfg.get("user", "")).strip()
        self.password_file = str(db_cfg.get("password_file", "")).strip()
        self.password = read_secret_file(self.password_file) or str(db_cfg.get("password", ""))
        self.database = self._identifier(str(db_cfg.get("database", "carcharger")))
        self.live_table = self._identifier(str(tesla_cfg.get("live_table", DEFAULT_TESLA_LIVE_TABLE)))
        self.history_table = self._identifier(str(tesla_cfg.get("history_table", DEFAULT_TESLA_HISTORY_TABLE)))
        self.last_error: str | None = None
        self._ready = False

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ValueError(f"Invalid SQL identifier: {value}")
        return value

    @staticmethod
    def _sql_text(value: Any) -> str:
        if value is None:
            return "NULL"
        # Quote-doubling ('') is honored in every sql_mode including
        # NO_BACKSLASH_ESCAPES, so a quote in the data can never terminate the
        # string literal regardless of server configuration.
        escaped = str(value).replace("\x00", "").replace("\\", "\\\\").replace("'", "''")
        return f"'{escaped}'"

    @staticmethod
    def _sql_number(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid SQL number: {value!r}")
        if not math.isfinite(number):
            return "NULL"
        return repr(number)

    @staticmethod
    def _db_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text or text.upper() == "NULL":
            return None
        return float(text)

    def _mysql_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.password:
            env["MYSQL_PWD"] = self.password
        return env

    def _base_cmd(self) -> list[str]:
        cmd = ["mysql", "--batch", "--raw", "--skip-column-names"]
        if self.socket:
            cmd.append(f"--socket={self.socket}")
        else:
            cmd.extend(["-h", self.host, "-P", str(self.port)])
        if self.user:
            cmd.extend(["-u", self.user])
        return cmd

    def _run_sql(self, sql: str) -> str:
        proc = subprocess.run(
            self._base_cmd(),
            input=sql,
            text=True,
            capture_output=True,
            env=self._mysql_env(),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "mysql command failed").strip())
        return proc.stdout.strip()

    def ensure_ready(self) -> bool:
        if not self.enabled:
            return False
        if self._ready:
            return True

        statements = []
        if self.bootstrap:
            statements.append(
                f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            )
        statements.append(f"USE `{self.database}`;")
        statements.append(
            f"""
CREATE TABLE IF NOT EXISTS `{self.live_table}` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sampled_at DATETIME NOT NULL,
  timezone_name VARCHAR(64) NOT NULL DEFAULT 'UTC',
  site_name VARCHAR(128) NULL,
  grid_status VARCHAR(32) NULL,
  storm_mode_active TINYINT(1) NOT NULL DEFAULT 0,
  solar_generation_w DECIMAL(12,2) NULL,
  home_consumption_w DECIMAL(12,2) NULL,
  powerwall_level_pct DECIMAL(6,2) NULL,
  grid_import_w DECIMAL(12,2) NULL,
  grid_export_w DECIMAL(12,2) NULL,
  raw_json LONGTEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sampled_at (sampled_at),
  KEY idx_sampled_at (sampled_at)
);
"""
        )
        statements.append(
            f"""
CREATE TABLE IF NOT EXISTS `{self.history_table}` (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  bucket_start DATETIME NOT NULL,
  bucket_end DATETIME NULL,
  period_name VARCHAR(16) NOT NULL,
  timezone_name VARCHAR(64) NOT NULL DEFAULT 'UTC',
  solar_generation_wh DECIMAL(14,3) NULL,
  home_consumption_wh DECIMAL(14,3) NULL,
  battery_charge_wh DECIMAL(14,3) NULL,
  battery_discharge_wh DECIMAL(14,3) NULL,
  grid_import_wh DECIMAL(14,3) NULL,
  grid_export_wh DECIMAL(14,3) NULL,
  raw_json LONGTEXT NULL,
  imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_bucket_period (bucket_start, period_name),
  KEY idx_bucket_start (bucket_start),
  KEY idx_period_name (period_name)
);
"""
        )

        try:
            self._run_sql("\n".join(statements))
            self._ready = True
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def insert_live_sample(self, sampled_at: datetime, metrics: dict[str, Any]) -> bool:
        if not self.ensure_ready():
            return False
        sql = f"""
USE `{self.database}`;
INSERT INTO `{self.live_table}` (
  sampled_at,
  timezone_name,
  site_name,
  grid_status,
  storm_mode_active,
  solar_generation_w,
  home_consumption_w,
  powerwall_level_pct,
  grid_import_w,
  grid_export_w,
  raw_json
) VALUES (
  {sql_datetime(sampled_at)},
  'UTC',
  {self._sql_text(metrics.get('site_name'))},
  {self._sql_text(metrics.get('grid_status'))},
  {1 if metrics.get('storm_mode_active') else 0},
  {self._sql_number(metrics.get('solar_generation_w'))},
  {self._sql_number(metrics.get('home_consumption_w'))},
  {self._sql_number(metrics.get('powerwall_level_pct'))},
  {self._sql_number(metrics.get('grid_import_w'))},
  {self._sql_number(metrics.get('grid_export_w'))},
  {self._sql_text(json.dumps(metrics.get('raw_live_status') or {}, separators=(',', ':')))}
)
ON DUPLICATE KEY UPDATE
  site_name = VALUES(site_name),
  grid_status = VALUES(grid_status),
  storm_mode_active = VALUES(storm_mode_active),
  solar_generation_w = VALUES(solar_generation_w),
  home_consumption_w = VALUES(home_consumption_w),
  powerwall_level_pct = VALUES(powerwall_level_pct),
  grid_import_w = VALUES(grid_import_w),
  grid_export_w = VALUES(grid_export_w),
  raw_json = VALUES(raw_json);
"""
        try:
            self._run_sql(sql)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def upsert_history_bucket(self, bucket: dict[str, Any]) -> bool:
        if not self.ensure_ready():
            return False
        sql = f"""
USE `{self.database}`;
INSERT INTO `{self.history_table}` (
  bucket_start,
  bucket_end,
  period_name,
  timezone_name,
  solar_generation_wh,
  home_consumption_wh,
  battery_charge_wh,
  battery_discharge_wh,
  grid_import_wh,
  grid_export_wh,
  raw_json
) VALUES (
  {sql_datetime(bucket.get('bucket_start'))},
  {sql_datetime(bucket.get('bucket_end'))},
  {self._sql_text(bucket.get('period_name'))},
  {self._sql_text(bucket.get('timezone_name') or 'UTC')},
  {self._sql_number(bucket.get('solar_generation_wh'))},
  {self._sql_number(bucket.get('home_consumption_wh'))},
  {self._sql_number(bucket.get('battery_charge_wh'))},
  {self._sql_number(bucket.get('battery_discharge_wh'))},
  {self._sql_number(bucket.get('grid_import_wh'))},
  {self._sql_number(bucket.get('grid_export_wh'))},
  {self._sql_text(json.dumps(bucket.get('raw_json') or {}, separators=(',', ':')))}
)
ON DUPLICATE KEY UPDATE
  bucket_end = VALUES(bucket_end),
  timezone_name = VALUES(timezone_name),
  solar_generation_wh = VALUES(solar_generation_wh),
  home_consumption_wh = VALUES(home_consumption_wh),
  battery_charge_wh = VALUES(battery_charge_wh),
  battery_discharge_wh = VALUES(battery_discharge_wh),
  grid_import_wh = VALUES(grid_import_wh),
  grid_export_wh = VALUES(grid_export_wh),
  raw_json = VALUES(raw_json);
"""
        try:
            self._run_sql(sql)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def counts(self) -> dict[str, int]:
        if not self.ensure_ready():
            return {"live_samples": 0, "history_buckets": 0}
        sql = f"""
USE `{self.database}`;
SELECT
  (SELECT COUNT(*) FROM `{self.live_table}`),
  (SELECT COUNT(*) FROM `{self.history_table}`);
"""
        try:
            output = self._run_sql(sql)
            cols = (output.splitlines()[0] if output else "").split("\t")
            self.last_error = None
            if len(cols) < 2:
                return {"live_samples": 0, "history_buckets": 0}
            return {"live_samples": int(cols[0]), "history_buckets": int(cols[1])}
        except Exception as exc:
            self.last_error = str(exc)
            return {"live_samples": 0, "history_buckets": 0}

    def query_live_series(
        self,
        start: datetime,
        end: datetime,
        bucket_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.ensure_ready():
            return []
        if bucket_seconds is None or int(bucket_seconds) <= 0:
            sql = f"""
USE `{self.database}`;
SELECT
  DATE_FORMAT(sampled_at, '%Y-%m-%dT%H:%i:%s'),
  solar_generation_w,
  home_consumption_w,
  powerwall_level_pct,
  grid_import_w,
  grid_export_w
FROM `{self.live_table}`
WHERE sampled_at >= {sql_datetime(start)} AND sampled_at <= {sql_datetime(end)}
ORDER BY sampled_at ASC;
"""
        else:
            bucket_seconds = max(60, int(bucket_seconds))
            sql = f"""
USE `{self.database}`;
SELECT
  FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(sampled_at) / {bucket_seconds}) * {bucket_seconds}),
  AVG(solar_generation_w),
  AVG(home_consumption_w),
  AVG(powerwall_level_pct),
  AVG(grid_import_w),
  AVG(grid_export_w)
FROM `{self.live_table}`
WHERE sampled_at >= {sql_datetime(start)} AND sampled_at <= {sql_datetime(end)}
GROUP BY 1
ORDER BY 1 ASC;
"""
        try:
            output = self._run_sql(sql)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            return []

        rows: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            rows.append(
                {
                    "timestamp": f"{cols[0].replace(' ', 'T')}+00:00",
                    "solar_generation_w": self._db_float(cols[1]),
                    "home_consumption_w": self._db_float(cols[2]),
                    "powerwall_level_pct": self._db_float(cols[3]),
                    "grid_import_w": self._db_float(cols[4]),
                    "grid_export_w": self._db_float(cols[5]),
                }
            )
        return rows

    def query_history_series(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if not self.ensure_ready():
            return []
        sql = f"""
USE `{self.database}`;
SELECT
  DATE_FORMAT(bucket_start, '%Y-%m-%dT%H:%i:%s'),
  COALESCE(solar_generation_wh, 0),
  COALESCE(home_consumption_wh, 0),
  COALESCE(grid_import_wh, 0),
  COALESCE(grid_export_wh, 0),
  COALESCE(battery_charge_wh, 0),
  COALESCE(battery_discharge_wh, 0)
FROM `{self.history_table}`
WHERE bucket_start >= {sql_datetime(start)} AND bucket_start <= {sql_datetime(end)}
ORDER BY bucket_start ASC;
"""
        try:
            output = self._run_sql(sql)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            return []

        rows: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            rows.append(
                {
                    "timestamp": f"{cols[0]}+00:00",
                    "solar_generation_wh": self._db_float(cols[1]) or 0.0,
                    "home_consumption_wh": self._db_float(cols[2]) or 0.0,
                    "grid_import_wh": self._db_float(cols[3]) or 0.0,
                    "grid_export_wh": self._db_float(cols[4]) or 0.0,
                    "battery_charge_wh": self._db_float(cols[5]) or 0.0,
                    "battery_discharge_wh": self._db_float(cols[6]) or 0.0,
                    "powerwall_level_pct": None,
                }
            )
        return rows


class TeslaEnergyMonitor:
    def __init__(self, raw_cfg: dict[str, Any] | None, database_cfg: dict[str, Any] | None = None):
        self.cfg = load_tesla_config(raw_cfg)
        self.client_secret_file = Path(str(self.cfg["client_secret_file"]))
        self.token_file = Path(str(self.cfg["token_file"]))
        self.cache_file = Path(str(self.cfg["cache_file"]))
        self.sync_state_file = Path(str(self.cfg["sync_state_file"]))
        self.store = TeslaEnergyStore(database_cfg, self.cfg)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def client_id(self) -> str:
        return str(self.cfg.get("client_id") or "").strip()

    @property
    def client_secret(self) -> str:
        return read_secret_file(self.client_secret_file)

    @property
    def redirect_uri(self) -> str:
        return str(self.cfg.get("redirect_uri") or "").strip()

    @property
    def audience(self) -> str:
        return str(self.cfg.get("audience") or DEFAULT_TESLA_AUDIENCE).strip()

    @property
    def api_base_url(self) -> str:
        return str(self.cfg.get("api_base_url") or DEFAULT_TESLA_API_BASE_URL).rstrip("/")

    @property
    def auth_url(self) -> str:
        return str(self.cfg.get("auth_url") or DEFAULT_TESLA_AUTH_URL).strip()

    @property
    def token_url(self) -> str:
        return str(self.cfg.get("token_url") or DEFAULT_TESLA_TOKEN_URL).strip()

    @property
    def scope(self) -> str:
        return str(self.cfg.get("scope") or DEFAULT_TESLA_SCOPE).strip()

    @property
    def partner_domain(self) -> str:
        return str(self.cfg.get("partner_domain") or "").strip()

    @property
    def partner_public_key_file(self) -> Path:
        return Path(str(self.cfg.get("partner_public_key_file") or DEFAULT_TESLA_PARTNER_PUBLIC_KEY_FILE))

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    @staticmethod
    def valid_partner_domain(value: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9.-]+", value or ""))

    def partner_public_key_url(self) -> str | None:
        if not self.partner_domain:
            return None
        return f"https://{self.partner_domain}/.well-known/appspecific/com.tesla.3p.public-key.pem"

    def partner_public_key_local_status(self) -> dict[str, Any]:
        path = self.partner_public_key_file
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        return {
            "path": str(path),
            "exists": path.exists(),
            "valid_pem": text.startswith("-----BEGIN PUBLIC KEY-----") and text.endswith("-----END PUBLIC KEY-----"),
        }

    @staticmethod
    def _looks_like_partner_setup_error(message: str | None) -> bool:
        text = str(message or "").lower()
        return any(
            needle in text
            for needle in (
                "must be registered",
                "allowed origin",
                "partner_accounts",
                "public key",
            )
        )

    def load_token_bundle(self) -> dict[str, Any]:
        data = load_json_file(self.token_file, {})
        return data if isinstance(data, dict) else {}

    def save_token_bundle(self, data: dict[str, Any]) -> None:
        save_json_file(self.token_file, data)

    def load_sync_state(self) -> dict[str, Any]:
        state = load_json_file(self.sync_state_file, {})
        if not isinstance(state, dict):
            state = {}
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        if state.get("month_key") != month_key:
            state = {
                "month_key": month_key,
                "requests_used": 0,
                "last_request_at": None,
                "last_live_sync_at": None,
                "last_site_info_at": None,
                "last_history_sync_at": None,
                "last_history_end_date": None,
                "last_error": None,
                "sync_failing_since": None,
                "consecutive_sync_failures": 0,
            }
            self.save_sync_state(state)
        return state

    def save_sync_state(self, data: dict[str, Any]) -> None:
        save_json_file(self.sync_state_file, data)

    def _merge_request_usage(self, state: dict[str, Any]) -> dict[str, Any]:
        latest = self.load_sync_state()
        state["requests_used"] = int(latest.get("requests_used") or state.get("requests_used") or 0)
        state["last_request_at"] = latest.get("last_request_at") or state.get("last_request_at")
        return state

    @staticmethod
    def _month_end(now: datetime) -> datetime:
        first_next_month = datetime(now.year + (1 if now.month == 12 else 0), 1 if now.month == 12 else now.month + 1, 1, tzinfo=timezone.utc)
        return first_next_month

    def _active_solar_seconds_remaining(self, now: datetime) -> float:
        """Remaining seconds within the active polling window for the rest of this month."""
        start_h = int(self.cfg.get("sync_active_start_hour") or 0)
        end_h   = int(self.cfg.get("sync_active_end_hour") or 24)
        hours_per_day = max(0, end_h - start_h)
        if hours_per_day <= 0:
            return 1.0
        now_local = now.astimezone()
        month_end_local = self._month_end(now).astimezone()
        # Remaining solar seconds today
        window_end_today = now_local.replace(hour=end_h, minute=0, second=0, microsecond=0)
        window_start_today = now_local.replace(hour=start_h, minute=0, second=0, microsecond=0)
        if now_local < window_start_today:
            today_solar = float(hours_per_day * 3600)
        elif now_local < window_end_today:
            today_solar = (window_end_today - now_local).total_seconds()
        else:
            today_solar = 0.0
        # Full solar days from tomorrow through end of month
        tomorrow = (now_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        full_days = max(0, (month_end_local.date() - tomorrow.date()).days)
        return today_solar + full_days * hours_per_day * 3600

    def _temporary_live_poll_interval_seconds(self) -> int | None:
        value = self.cfg.get("temporary_live_poll_interval_seconds")
        if value in (None, ""):
            return None
        return max(30, int(value))

    def request_budget_status(self) -> dict[str, Any]:
        state = self.load_sync_state()
        now = datetime.now(timezone.utc)
        total = max(1, int(self.cfg.get("monthly_request_budget") or 5000))
        reserve = max(0, int(self.cfg.get("monthly_request_reserve") or 200))
        usable = max(1, total - reserve)
        used = max(0, int(state.get("requests_used") or 0))
        remaining = max(0, usable - used)
        month_end = self._month_end(now)
        start_h = int(self.cfg.get("sync_active_start_hour") or 0)
        end_h   = int(self.cfg.get("sync_active_end_hour") or 24)
        active_window = (start_h != 0 or end_h != 24)
        temporary_poll_interval = self._temporary_live_poll_interval_seconds()
        if active_window:
            # Spread remaining budget only across remaining solar hours
            seconds_remaining = max(1.0, self._active_solar_seconds_remaining(now))
        else:
            seconds_remaining = max(1.0, (month_end - now).total_seconds())
        spacing_seconds = max(60.0, seconds_remaining / max(1, remaining)) if remaining else seconds_remaining
        if temporary_poll_interval is not None:
            spacing_seconds = float(temporary_poll_interval)
        return {
            "month_key": state["month_key"],
            "budget_total": total,
            "budget_reserve": reserve,
            "budget_usable": usable,
            "requests_used": used,
            "requests_remaining": remaining,
            "seconds_remaining_in_month": int((month_end - now).total_seconds()),
            "recommended_spacing_seconds": int(round(spacing_seconds)),
            "next_month_at": month_end.isoformat(),
            "temporary_live_poll_interval_seconds": temporary_poll_interval,
            "temporary_allow_request_budget_overrun": bool(
                self.cfg.get("temporary_allow_request_budget_overrun", False)
            ),
        }

    def _record_request_usage(self) -> None:
        state = self.load_sync_state()
        state["requests_used"] = int(state.get("requests_used") or 0) + 1
        state["last_request_at"] = datetime.now(timezone.utc).isoformat()
        self.save_sync_state(state)

    def _can_spend_request(self) -> bool:
        return bool(self.cfg.get("temporary_allow_request_budget_overrun", False)) or (
            self.request_budget_status()["requests_remaining"] > 0
        )

    def _persist_token_response(self, token_payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.load_token_bundle()
        now = datetime.now(timezone.utc)
        expires_in = int(token_payload.get("expires_in") or 0)
        refresh_token = token_payload.get("refresh_token") or existing.get("refresh_token")
        merged = {
            "access_token": token_payload.get("access_token"),
            "refresh_token": refresh_token,
            "token_type": token_payload.get("token_type") or existing.get("token_type") or "Bearer",
            "scope": token_payload.get("scope") or existing.get("scope") or self.scope,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=max(0, expires_in))).isoformat() if expires_in else None,
            "oauth_state": existing.get("oauth_state"),
            "oauth_state_created_at": existing.get("oauth_state_created_at"),
            "last_energy_site_id": existing.get("last_energy_site_id"),
        }
        self.save_token_bundle(merged)
        return merged

    def store_refresh_token(self, refresh_token: str) -> None:
        token_bundle = self.load_token_bundle()
        token_bundle["refresh_token"] = refresh_token.strip()
        token_bundle.setdefault("scope", self.scope)
        self.save_token_bundle(token_bundle)

    def status_summary(self) -> dict[str, Any]:
        token_bundle = self.load_token_bundle()
        cache = load_json_file(self.cache_file, {})
        sync_state = self.load_sync_state()
        expires_at = token_bundle.get("expires_at")
        authorized = bool(token_bundle.get("refresh_token"))
        last_updated = cache.get("last_updated") if isinstance(cache, dict) else None
        counts = self.store.counts() if self.store.enabled else {"live_samples": 0, "history_buckets": 0}
        partner_key = self.partner_public_key_local_status()
        last_sync_error = sync_state.get("last_error")
        tls_cert_file = Path(str(self.cfg.get("tls_cert_file") or ""))
        tls_key_file = Path(str(self.cfg.get("tls_key_file") or ""))
        return {
            "enabled": self.enabled,
            "configured": self.configured(),
            "client_secret_configured": secret_is_configured(self.client_secret_file),
            "authorized": authorized,
            "redirect_uri": self.redirect_uri,
            "energy_site_id": str(self.cfg.get("energy_site_id") or token_bundle.get("last_energy_site_id") or ""),
            "last_updated": last_updated,
            "access_token_expires_at": expires_at,
            "database_ready": self.store.ensure_ready() if self.store.enabled else False,
            "live_samples": counts["live_samples"],
            "history_buckets": counts["history_buckets"],
            "last_sync_error": last_sync_error,
            "sync_failing_since": sync_state.get("sync_failing_since"),
            "consecutive_sync_failures": int(sync_state.get("consecutive_sync_failures") or 0),
            "partner_domain": self.partner_domain,
            "partner_public_key_file": partner_key["path"],
            "partner_public_key_url": self.partner_public_key_url(),
            "partner_public_key_local_ready": bool(partner_key["exists"] and partner_key["valid_pem"]),
            "partner_registration_required": self._looks_like_partner_setup_error(last_sync_error),
            "public_http_port": int(self.cfg.get("public_http_port") or 8078),
            "public_https_port": int(self.cfg.get("public_https_port") or 9443),
            "tls_cert_ready": tls_cert_file.exists() and tls_key_file.exists(),
            "request_budget": self.request_budget_status(),
        }

    def build_authorize_url(self) -> str | None:
        if not self.configured():
            return None
        state = secrets.token_urlsafe(24)
        token_bundle = self.load_token_bundle()
        token_bundle["oauth_state"] = state
        token_bundle["oauth_state_created_at"] = datetime.now(timezone.utc).isoformat()
        self.save_token_bundle(token_bundle)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.scope,
                "state": state,
                "audience": self.audience,
            }
        )
        return f"{self.auth_url}?{query}"

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        body = urlencode(payload).encode("utf-8")
        request = Request(
            self.token_url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Tesla token request failed: {detail or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"Tesla token request failed: {exc.reason}") from exc

        if not isinstance(parsed, dict) or not parsed.get("access_token"):
            raise RuntimeError("Tesla token response did not include an access token")
        return parsed

    def exchange_code(self, code: str, state: str | None = None) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Tesla Fleet API is not fully configured")
        token_bundle = self.load_token_bundle()
        expected_state = token_bundle.get("oauth_state")
        if state and expected_state and state != expected_state:
            raise RuntimeError("Tesla OAuth state mismatch")
        parsed = self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code.strip(),
                "audience": self.audience,
                "redirect_uri": self.redirect_uri,
            }
        )
        token_bundle = self._persist_token_response(parsed)
        token_bundle.pop("oauth_state", None)
        token_bundle.pop("oauth_state_created_at", None)
        self.save_token_bundle(token_bundle)
        return token_bundle

    def refresh_access_token(self) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Tesla Fleet API is not fully configured")
        token_bundle = self.load_token_bundle()
        refresh_token = str(token_bundle.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("Tesla refresh token is not configured")
        parsed = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            }
        )
        return self._persist_token_response(parsed)

    def get_access_token(self) -> str:
        token_bundle = self.load_token_bundle()
        access_token = str(token_bundle.get("access_token") or "").strip()
        expires_at_raw = token_bundle.get("expires_at")
        if access_token and expires_at_raw:
            expires_at = parse_datetime(expires_at_raw)
            if expires_at and expires_at - datetime.now(timezone.utc) > timedelta(seconds=90):
                return access_token
        refreshed = self.refresh_access_token()
        return str(refreshed.get("access_token") or "").strip()

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._can_spend_request():
            raise RuntimeError("Tesla request budget exhausted for this month reserve window")
        token = self.get_access_token()
        return self._request_json_with_token(path, token=token, params=params, count_against_budget=True)

    def _request_json_with_token(
        self,
        path: str,
        token: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        count_against_budget: bool = False,
    ) -> Any:
        query = ""
        if params:
            filtered = {k: v for k, v in params.items() if v not in (None, "")}
            if filtered:
                query = "?" + urlencode(filtered)
        url = f"{self.api_base_url}{path}{query}"
        payload = None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            url,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=25) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if count_against_budget and exc.code < 500:
                self._record_request_usage()
            raise RuntimeError(f"Tesla API request failed for {path}: {detail or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"Tesla API request failed for {path}: {exc.reason}") from exc
        if count_against_budget:
            self._record_request_usage()
        return parsed

    def get_partner_access_token(self) -> str:
        if not self.configured():
            raise RuntimeError("Tesla Fleet API is not fully configured")
        parsed = self._token_request(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "openid energy_device_data",
                "audience": self.audience,
            }
        )
        return str(parsed.get("access_token") or "").strip()

    def register_partner_account(self) -> dict[str, Any]:
        if not self.partner_domain:
            raise RuntimeError("Tesla partner domain is not configured")
        if not self.valid_partner_domain(self.partner_domain):
            raise RuntimeError("Tesla partner domain must be a bare lowercase hostname without scheme or port")
        key_status = self.partner_public_key_local_status()
        if not key_status["exists"] or not key_status["valid_pem"]:
            raise RuntimeError("Tesla partner public key file is missing or invalid")
        token = self.get_partner_access_token()
        payload = self._request_json_with_token(
            "/api/1/partner_accounts",
            token=token,
            method="POST",
            body={"domain": self.partner_domain},
            count_against_budget=False,
        )
        sync_state = self.load_sync_state()
        sync_state["last_error"] = None
        self.save_sync_state(sync_state)
        return payload if isinstance(payload, dict) else {"response": payload}

    @staticmethod
    def _unwrap_response(payload: Any) -> Any:
        if isinstance(payload, dict) and "response" in payload:
            return payload["response"]
        return payload

    def list_products(self) -> list[dict[str, Any]]:
        payload = self._unwrap_response(self._request_json("/api/1/products"))
        return payload if isinstance(payload, list) else []

    def resolve_energy_site_id(self) -> str:
        explicit = str(self.cfg.get("energy_site_id") or "").strip()
        if explicit:
            return explicit
        token_bundle = self.load_token_bundle()
        remembered = str(token_bundle.get("last_energy_site_id") or "").strip()
        if remembered:
            return remembered
        products = self.list_products()
        for item in products:
            if isinstance(item, dict) and item.get("energy_site_id") not in (None, ""):
                token_bundle["last_energy_site_id"] = str(item["energy_site_id"])
                self.save_token_bundle(token_bundle)
                return str(item["energy_site_id"])
        for item in products:
            if not isinstance(item, dict):
                continue
            resource_type = str(item.get("resource_type") or "").lower()
            if resource_type in {"battery", "solar"} and item.get("id") not in (None, ""):
                token_bundle["last_energy_site_id"] = str(item["id"])
                self.save_token_bundle(token_bundle)
                return str(item["id"])
        raise RuntimeError("Tesla energy site ID could not be determined from products")

    def fetch_site_info(self, energy_site_id: str) -> dict[str, Any]:
        payload = self._unwrap_response(self._request_json(f"/api/1/energy_sites/{energy_site_id}/site_info"))
        return payload if isinstance(payload, dict) else {}

    def fetch_live_status(self, energy_site_id: str) -> dict[str, Any]:
        payload = self._unwrap_response(self._request_json(f"/api/1/energy_sites/{energy_site_id}/live_status"))
        return payload if isinstance(payload, dict) else {}

    def fetch_energy_history(
        self,
        energy_site_id: str,
        start_date: date,
        end_date: date,
        period: str,
        time_zone_name: str = "UTC",
    ) -> Any:
        return self._unwrap_response(
            self._request_json(
                f"/api/1/energy_sites/{energy_site_id}/calendar_history",
                params={
                    "kind": "energy",
                    "start_date": tesla_history_datetime(start_date),
                    "end_date": tesla_history_datetime(end_date, end_of_day=True),
                    "period": period,
                    "time_zone": time_zone_name,
                },
            )
        )

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _pick_float(self, payload: dict[str, Any], keys: list[str]) -> float | None:
        for key in keys:
            if key in payload:
                parsed = self._to_float(payload.get(key))
                if parsed is not None:
                    return parsed
        return None

    def _sum_keys(self, payload: dict[str, Any], keys: list[str]) -> float | None:
        values = [self._to_float(payload.get(key)) for key in keys if key in payload]
        valid = [value for value in values if value is not None]
        return round(sum(valid), 3) if valid else None

    def _extract_metrics(self, live_status: dict[str, Any], site_info: dict[str, Any]) -> dict[str, Any]:
        solar_generation_w = self._pick_float(
            live_status,
            ["solar_power", "solar_power_w", "solar_generation", "solar_generation_w"],
        )
        home_consumption_w = self._pick_float(
            live_status,
            ["load_power", "load_power_w", "site_load_power", "home_consumption_w"],
        )
        powerwall_level_pct = self._pick_float(
            live_status,
            ["percentage_charged", "battery_level", "battery_percentage", "state_of_energy"],
        )
        grid_import_w = self._pick_float(
            live_status,
            ["grid_import_power", "grid_import_power_w", "grid_import_w"],
        )
        grid_export_w = self._pick_float(
            live_status,
            ["grid_export_power", "grid_export_power_w", "grid_export_w"],
        )
        grid_power = self._pick_float(live_status, ["grid_power", "grid_power_w"])
        if grid_import_w is None and grid_export_w is None and grid_power is not None:
            if grid_power >= 0:
                grid_import_w = grid_power
                grid_export_w = 0.0
            else:
                grid_import_w = 0.0
                grid_export_w = abs(grid_power)

        return {
            "solar_generation_w": solar_generation_w,
            "home_consumption_w": home_consumption_w,
            "powerwall_level_pct": powerwall_level_pct,
            "grid_import_w": grid_import_w,
            "grid_export_w": grid_export_w,
            "grid_status": str(live_status.get("grid_status") or site_info.get("grid_status") or ""),
            "site_name": str(site_info.get("site_name") or site_info.get("site_nameplate") or ""),
            "storm_mode_active": bool(
                live_status.get("storm_mode_active")
                or live_status.get("storm_mode_enabled")
                or site_info.get("storm_mode_enabled")
            ),
            "raw_live_status": live_status,
        }

    def _iter_history_rows(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("time_series", "series", "records", "results", "data", "periods"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _normalize_history_row(self, row: dict[str, Any], period: str) -> dict[str, Any] | None:
        bucket_start = None
        for key in ("timestamp", "time", "start_date", "start_time", "start_at", "date"):
            bucket_start = parse_datetime(row.get(key))
            if bucket_start is not None:
                break
        if bucket_start is None:
            return None

        bucket_end = None
        for key in ("end_date", "end_time", "end_at"):
            bucket_end = parse_datetime(row.get(key))
            if bucket_end is not None:
                break
        if bucket_end is None:
            if period == "day":
                bucket_end = bucket_start + timedelta(days=1)
            elif period == "month":
                bucket_end = bucket_start + timedelta(minutes=30)

        solar_generation_wh = self._pick_float(
            row,
            ["solar_generation_wh", "solar_energy_wh", "solar_energy", "solar", "solar_energy_exported"],
        )
        if solar_generation_wh is None:
            solar_generation_wh = self._sum_keys(
                row,
                [
                    "solar_to_home",
                    "solar_to_battery",
                    "solar_to_grid",
                    "solar_to_load",
                    "consumer_energy_imported_from_solar",
                    "grid_energy_exported_from_solar",
                    "battery_energy_imported_from_solar",
                ],
            )

        home_consumption_wh = self._pick_float(
            row,
            [
                "home_consumption_wh",
                "consumer_energy_wh",
                "consumption_wh",
                "load_energy_wh",
                "home_energy",
                "total_home_usage",
            ],
        )
        if home_consumption_wh is None:
            home_consumption_wh = self._sum_keys(
                row,
                [
                    "solar_to_home",
                    "battery_to_home",
                    "grid_to_home",
                    "generator_to_home",
                    "consumer_energy_imported_from_solar",
                    "consumer_energy_imported_from_battery",
                    "consumer_energy_imported_from_grid",
                    "consumer_energy_imported_from_generator",
                ],
            )

        grid_import_wh = self._pick_float(
            row,
            [
                "grid_import_wh",
                "grid_energy_imported_wh",
                "grid_energy_imported",
                "grid_to_home",
                "energy_imported",
                "consumer_energy_imported_from_grid",
            ],
        )
        if grid_import_wh is None:
            grid_import_wh = self._sum_keys(row, ["grid_to_home", "grid_to_battery"])

        grid_export_wh = self._pick_float(
            row,
            ["grid_export_wh", "grid_energy_exported_wh", "grid_energy_exported", "energy_exported"],
        )
        if grid_export_wh is None:
            grid_export_wh = self._sum_keys(
                row,
                [
                    "solar_to_grid",
                    "battery_to_grid",
                    "grid_energy_exported_from_solar",
                    "grid_energy_exported_from_battery",
                    "grid_energy_exported_from_generator",
                ],
            )

        battery_charge_wh = self._pick_float(
            row,
            [
                "battery_charge_wh",
                "battery_energy_imported_wh",
                "battery_to_charge_wh",
                "total_battery_charge",
            ],
        )
        if battery_charge_wh is None:
            battery_charge_wh = self._sum_keys(
                row,
                [
                    "solar_to_battery",
                    "grid_to_battery",
                    "battery_energy_imported_from_solar",
                    "battery_energy_imported_from_grid",
                    "battery_energy_imported_from_generator",
                ],
            )

        battery_discharge_wh = self._pick_float(
            row,
            [
                "battery_discharge_wh",
                "battery_energy_exported_wh",
                "battery_energy_exported",
                "battery_output_wh",
                "total_battery_discharge",
            ],
        )
        if battery_discharge_wh is None:
            battery_discharge_wh = self._sum_keys(row, ["battery_to_home", "battery_to_grid"])

        return {
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "period_name": period,
            "timezone_name": "UTC",
            "solar_generation_wh": solar_generation_wh,
            "home_consumption_wh": home_consumption_wh,
            "battery_charge_wh": battery_charge_wh,
            "battery_discharge_wh": battery_discharge_wh,
            "grid_import_wh": grid_import_wh,
            "grid_export_wh": grid_export_wh,
            "raw_json": row,
        }

    def _trim_cache_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_age = timedelta(hours=max(1, int(self.cfg.get("history_hours") or 24)))
        cutoff = datetime.now(timezone.utc) - max_age
        return [
            item
            for item in history
            if isinstance(item, dict)
            and parse_datetime(item.get("timestamp")) is not None
            and parse_datetime(item.get("timestamp")) >= cutoff
        ]

    def _downsample_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_points = max(24, int(self.cfg.get("history_max_points") or 288))
        if len(history) <= max_points:
            return history
        last_index = len(history) - 1
        picks = {round(i * last_index / (max_points - 1)) for i in range(max_points)}
        return [item for idx, item in enumerate(history) if idx in picks]

    def _append_cache_history(self, history: list[dict[str, Any]], timestamp: datetime, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        sample = {
            "timestamp": timestamp.isoformat(),
            "solar_generation_w": metrics.get("solar_generation_w"),
            "home_consumption_w": metrics.get("home_consumption_w"),
            "powerwall_level_pct": metrics.get("powerwall_level_pct"),
            "grid_import_w": metrics.get("grid_import_w"),
            "grid_export_w": metrics.get("grid_export_w"),
        }
        updated = list(history)
        if updated and updated[-1].get("timestamp") == sample["timestamp"]:
            updated[-1] = sample
        else:
            updated.append(sample)
        return self._trim_cache_history(updated)

    def import_history(
        self,
        start_date: date,
        end_date: date,
        period: str = "day",
        time_zone_name: str = "UTC",
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Tesla Fleet energy is disabled")
        if not self.configured():
            raise RuntimeError("Tesla Fleet API is not fully configured")
        if not self.load_token_bundle().get("refresh_token"):
            raise RuntimeError("Tesla refresh token is not configured")
        energy_site_id = self.resolve_energy_site_id()
        request_period = "month" if period == "day" else period
        payload = self.fetch_energy_history(energy_site_id, start_date, end_date, request_period, time_zone_name)
        rows = self._iter_history_rows(payload)
        imported = 0
        skipped = 0
        for row in rows:
            normalized = self._normalize_history_row(row, request_period)
            if normalized is None:
                skipped += 1
                continue
            if self.store.upsert_history_bucket(normalized):
                imported += 1
            else:
                skipped += 1
        return {
            "ok": True,
            "energy_site_id": energy_site_id,
            "period": request_period,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "fetched_rows": len(rows),
            "imported_rows": imported,
            "skipped_rows": skipped,
            "last_error": self.store.last_error,
        }

    def import_recent_history(self, days: int = 90) -> dict[str, Any]:
        days = max(1, min(int(days), 365))
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=days)
        period = "day" if days > 14 else "day"
        return self.import_history(start_date, end_date, period=period, time_zone_name="UTC")

    def chart_history(
        self,
        range_name: str = "6h",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        end_dt = parse_datetime(end) if end else datetime.now(timezone.utc)
        if end_dt is None:
            end_dt = datetime.now(timezone.utc)
        start_dt = parse_datetime(start) if start else None

        if start_dt is None:
            mapping = {
                "2h": timedelta(hours=2),
                "4h": timedelta(hours=4),
                "6h": timedelta(hours=6),
                "12h": timedelta(hours=12),
                "1d": timedelta(days=1),
                "2d": timedelta(days=2),
                "3d": timedelta(days=3),
                "7d": timedelta(days=7),
                "21d": timedelta(days=21),
                "30d": timedelta(days=30),
                "90d": timedelta(days=90),
            }
            start_dt = end_dt - mapping.get(range_name, timedelta(hours=6))

        duration = max(timedelta(minutes=1), end_dt - start_dt)
        use_live = duration <= timedelta(days=7)
        if use_live:
            points = self.store.query_live_series(start_dt, end_dt, None)
            if points:
                return {
                    "source": "database",
                    "mode": "power",
                    "unit": "W",
                    "range": range_name,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "points": points,
                    "powerwall_supported": True,
                }

        points = self.store.query_history_series(start_dt, end_dt)
        if points:
            return {
                "source": "database",
                "mode": "energy",
                "unit": "Wh",
                "range": range_name,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "points": points,
                "powerwall_supported": False,
            }

        cache = load_json_file(self.cache_file, {})
        cached_points = self._downsample_history(
            cache.get("history") if isinstance(cache.get("history"), list) else []
        )
        return {
            "source": "cache",
            "mode": "power",
            "unit": "W",
            "range": range_name,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "points": cached_points,
            "powerwall_supported": True,
        }

    def _cached_status_payload(self) -> dict[str, Any]:
        summary = self.status_summary()
        cache = load_json_file(self.cache_file, {})
        if not isinstance(cache, dict):
            cache = {}
        return {
            **summary,
            "source": "none",
            "stale": False,
            "error": None,
            "metrics": cache.get("metrics") if isinstance(cache.get("metrics"), dict) else {},
            "history": self._downsample_history(
                cache.get("history") if isinstance(cache.get("history"), list) else []
            ),
            "site_info": cache.get("site_info") if isinstance(cache.get("site_info"), dict) else {},
            "last_updated": cache.get("last_updated"),
            "request_budget": self.request_budget_status(),
        }

    def read_status(self) -> dict[str, Any]:
        payload = self._cached_status_payload()
        if not self.enabled:
            return payload
        if not self.configured():
            payload["error"] = "Tesla Fleet API is not fully configured."
            return payload
        if not payload["authorized"]:
            payload["error"] = "Tesla Fleet API still needs a refresh token or OAuth code exchange."
            return payload
        payload["source"] = "database" if payload["live_samples"] or payload["history_buckets"] else payload["source"]
        if not payload["metrics"]:
            payload["stale"] = True
            payload["error"] = payload["error"] or payload.get("last_sync_error") or "Tesla sync has not populated any data yet."
        return payload

    def _early_refresh_due_at(self) -> datetime | None:
        request_path = BASE_DIR / "tesla_refresh_request.json"
        if not request_path.exists():
            return None
        try:
            data = load_json_file(request_path, {})
            requested_at_str = data.get("requested_at", "")
            if not requested_at_str:
                request_path.unlink(missing_ok=True)
                return None
            return datetime.fromisoformat(requested_at_str).astimezone()
        except Exception:
            try:
                request_path.unlink(missing_ok=True)
            except Exception:
                pass
        return None

    def _consume_early_refresh_request(self) -> bool:
        """Return True and clear the file if an early refresh was requested and is due."""
        request_path = BASE_DIR / "tesla_refresh_request.json"
        requested_at = self._early_refresh_due_at()
        if requested_at is None:
            return False
        if datetime.now().astimezone() >= requested_at:
            request_path.unlink(missing_ok=True)
            return True
        return False

    def sync_once(self, force_live: bool = False) -> dict[str, Any]:
        payload = self._cached_status_payload()
        if not self.enabled:
            payload["error"] = "Tesla Fleet energy is disabled."
            return payload
        if not self.configured():
            payload["error"] = "Tesla Fleet API is not fully configured."
            return payload
        if not payload["authorized"]:
            payload["error"] = "Tesla Fleet API still needs a refresh token or OAuth code exchange."
            return payload
        if not self.store.ensure_ready():
            payload["error"] = self.store.last_error or "Tesla MySQL tables are not ready."
            return payload

        state = self.load_sync_state()
        budget = self.request_budget_status()
        now = datetime.now(timezone.utc)
        spacing = timedelta(seconds=budget["recommended_spacing_seconds"])

        def last_seen(key: str) -> datetime | None:
            return parse_datetime(state.get(key))

        site_info_refresh = timedelta(hours=max(1, int(self.cfg.get("site_info_refresh_hours") or 24)))
        site_info = payload.get("site_info") if isinstance(payload.get("site_info"), dict) else {}
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}

        try:
            energy_site_id = self.resolve_energy_site_id()

            need_site_info = not site_info or last_seen("last_site_info_at") is None or (now - last_seen("last_site_info_at")) >= site_info_refresh
            if need_site_info:
                site_info = self.fetch_site_info(energy_site_id)
                state["last_site_info_at"] = now.isoformat()

            last_live_sync = last_seen("last_live_sync_at")
            should_live_sync = force_live or last_live_sync is None or (now - last_live_sync) >= spacing
            if should_live_sync:
                live_status = self.fetch_live_status(energy_site_id)
                metrics = self._extract_metrics(live_status, site_info if isinstance(site_info, dict) else {})
                required_live_metrics = ("solar_generation_w", "home_consumption_w", "powerwall_level_pct")
                if all(metrics.get(key) is None for key in required_live_metrics):
                    raise RuntimeError("Tesla live_status response did not include energy metrics")
                self.store.insert_live_sample(now, metrics)
                history = self._append_cache_history(
                    payload.get("history") if isinstance(payload.get("history"), list) else [],
                    now,
                    metrics,
                )
                save_json_file(
                    self.cache_file,
                    {
                        "last_updated": now.isoformat(),
                        "energy_site_id": energy_site_id,
                        "metrics": metrics,
                        "site_info": site_info,
                        "history": history,
                    },
                )
                state["last_live_sync_at"] = now.isoformat()

            # Automatic daily history import was removed 2026-06-10: live samples
            # land in MySQL continuously, and history can still be imported
            # manually via /api/tesla-energy/import-history.

            state["last_error"] = None
            state["sync_failing_since"] = None
            state["consecutive_sync_failures"] = 0
            state = self._merge_request_usage(state)
            self.save_sync_state(state)
            return self.read_status()
        except Exception as exc:
            state["last_error"] = str(exc)
            state["consecutive_sync_failures"] = int(state.get("consecutive_sync_failures") or 0) + 1
            if not state.get("sync_failing_since"):
                state["sync_failing_since"] = now.isoformat()
            state = self._merge_request_usage(state)
            self.save_sync_state(state)
            payload["stale"] = True
            payload["error"] = str(exc)
            return payload

    def run_sync_loop(self) -> None:
        # Polls 24/7. The solar-hours window restriction was removed 2026-06-10
        # because Tesla request volume is no longer a constraint.
        consecutive_failures = 0
        while True:
            # Check if controller requested an early confirmation pull
            force_live = self._consume_early_refresh_request()
            result = self.sync_once(force_live=force_live)
            error = result.get("error")
            now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if error:
                consecutive_failures += 1
                if consecutive_failures <= 10 or consecutive_failures % 10 == 0:
                    print(
                        f"{now_str} tesla_sync_error consecutive={consecutive_failures} "
                        f"error={str(error)[:300]}",
                        flush=True,
                    )
            else:
                if consecutive_failures:
                    print(
                        f"{now_str} tesla_sync_recovered after_failures={consecutive_failures}",
                        flush=True,
                    )
                consecutive_failures = 0

            temporary_poll_interval = self._temporary_live_poll_interval_seconds()
            sleep_seconds = (
                temporary_poll_interval
                if temporary_poll_interval is not None
                else max(30, int(self.cfg.get("sync_idle_sleep_seconds") or 60))
            )
            budget = self.request_budget_status()
            sleep_seconds = max(sleep_seconds, min(budget["recommended_spacing_seconds"], 6 * 3600))
            if error:
                # Retry failed pulls quickly so a brief Tesla cloud blip only
                # costs one sample, then back off during a long outage.
                sleep_seconds = 10 if consecutive_failures <= 6 else min(sleep_seconds, 30)
            due_at = self._early_refresh_due_at()
            if due_at is not None:
                seconds_to_due = (due_at - datetime.now().astimezone()).total_seconds()
                sleep_seconds = min(sleep_seconds, max(1, seconds_to_due))

            time.sleep(sleep_seconds)

    def read_status_legacy(self) -> dict[str, Any]:
        if not self.enabled:
            return self._cached_status_payload()
        if not self.configured():
            return self._cached_status_payload()
        return self.sync_once()
