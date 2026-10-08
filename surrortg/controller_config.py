"""Versioned, crash-safe Controller-local bootstrap configuration."""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import toml


SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path("/etc/srtg/controller.toml")
DEFAULT_SECRET_PATH = Path("/etc/srtg/controller.credential")
DEFAULT_LEGACY_PATH = Path("/etc/srtg/srtg.toml")


class ConfigurationError(ValueError):
    pass


class ConfigurationConflict(ConfigurationError):
    pass


class ChangeEffect(str, Enum):
    NONE = "none"
    RECONNECT = "reconnect_required"
    RESTART = "runtime_restart_required"


@dataclass(frozen=True)
class ControllerConfig:
    schema_version: int
    device_id: str
    signaling_endpoint: str
    game_id: str
    runtime_module: Optional[str] = None

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ConfigurationError("unsupported schema_version")
        for name in ("device_id", "signaling_endpoint", "game_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigurationError(f"{name} must be a non-empty string")
        if not self.signaling_endpoint.startswith(("http://", "https://")):
            raise ConfigurationError("signaling_endpoint must use http or https")
        if self.runtime_module is not None and (
            not isinstance(self.runtime_module, str) or not self.runtime_module.strip()
        ):
            raise ConfigurationError("runtime_module must be a non-empty string")

    @property
    def revision(self):
        raw = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    def sanitized(self):
        return {**asdict(self), "revision": self.revision}

    def legacy_runtime(self, credential):
        return {
            "device_id": self.device_id,
            "game_engine": {
                "url": self.signaling_endpoint,
                "id": self.game_id,
                "token": credential,
            },
        }


def classify_change(old, new):
    if old == new:
        return ChangeEffect.NONE
    if old.runtime_module != new.runtime_module or old.device_id != new.device_id:
        return ChangeEffect.RESTART
    return ChangeEffect.RECONNECT


class ControllerConfigurationStore:
    """One writable source of truth; a legacy file is import-only."""

    def __init__(self, config_path=DEFAULT_CONFIG_PATH, secret_path=DEFAULT_SECRET_PATH):
        self.config_path = Path(config_path)
        self.secret_path = Path(secret_path)
        self.last_good_path = self.config_path.with_suffix(self.config_path.suffix + ".last-good")

    def load(self):
        return self._parse_new(self.config_path)

    def load_secret(self):
        try:
            credential = self.secret_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConfigurationError("Controller credential is unavailable") from error
        if not credential:
            raise ConfigurationError("Controller credential is empty")
        return credential

    def sanitized(self):
        return self.load().sanitized()

    def update(self, config, expected_revision=None):
        if not isinstance(config, ControllerConfig):
            raise ConfigurationError("config must be ControllerConfig")
        current = self.load() if self.config_path.exists() else None
        if expected_revision is not None and (
            current is None or current.revision != expected_revision
        ):
            raise ConfigurationConflict("configuration revision has changed")
        effect = classify_change(current, config) if current else ChangeEffect.RESTART
        payload = toml.dumps({
            "schema_version": config.schema_version,
            "device_id": config.device_id,
            "signaling": {"endpoint": config.signaling_endpoint},
            "game": {"id": config.game_id},
            **({"runtime": {"module": config.runtime_module}} if config.runtime_module else {}),
        })
        self._atomic_write(self.config_path, payload, 0o640)
        try:
            written = self.load()
            if written != config:
                raise ConfigurationError("configuration verification failed")
            self._atomic_write(self.last_good_path, payload, 0o640)
        except Exception:
            if self.last_good_path.exists():
                self._atomic_write(
                    self.config_path,
                    self.last_good_path.read_text(encoding="utf-8"),
                    0o640,
                )
            raise
        return {"configuration": written.sanitized(), "effect": effect.value}

    def replace_secret(self, credential):
        if not isinstance(credential, str) or not credential.strip():
            raise ConfigurationError("credential must be a non-empty string")
        self._atomic_write(self.secret_path, credential.strip() + "\n", 0o600)
        if self.load_secret() != credential.strip():
            raise ConfigurationError("credential verification failed")
        return ChangeEffect.RECONNECT

    def import_legacy(self, legacy_path=DEFAULT_LEGACY_PATH):
        legacy = _parse_legacy(legacy_path)
        # Validate both artifacts before changing either destination.
        config, credential = legacy
        if self.config_path.exists():
            return self.load()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # A secret without config is harmless; config without its secret would
        # incorrectly shadow the still-usable legacy source on next startup.
        self.replace_secret(credential)
        try:
            self.update(config)
        except Exception:
            raise
        return config

    def _parse_new(self, path):
        try:
            raw = toml.load(path)
            return ControllerConfig(
                schema_version=raw["schema_version"],
                device_id=raw["device_id"],
                signaling_endpoint=raw["signaling"]["endpoint"],
                game_id=str(raw["game"]["id"]),
                runtime_module=raw.get("runtime", {}).get("module"),
            )
        except ConfigurationError:
            raise
        except Exception as error:
            raise ConfigurationError(f"invalid Controller configuration: {path}") from error

    @staticmethod
    def _atomic_write(path, data, mode):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def _parse_legacy(path):
    try:
        raw = toml.load(path)
        engine = raw["game_engine"]
        credential = str(engine["token"])
        game_id = engine.get("id")
        if game_id is None:
            if "/" not in credential:
                raise ConfigurationError("legacy combined credential is malformed")
            game_id, credential = credential.split("/", 1)
        elif "/" in credential:
            raise ConfigurationError("combined credential cannot be used with game_engine.id")
        config = ControllerConfig(
            schema_version=SCHEMA_VERSION,
            device_id=str(raw["device_id"]),
            signaling_endpoint=str(engine["url"]),
            game_id=str(game_id),
            runtime_module=raw.get("runtime", {}).get("module"),
        )
        if not credential:
            raise ConfigurationError("legacy credential is empty")
        return config, credential
    except ConfigurationError:
        raise
    except Exception as error:
        raise ConfigurationError(f"invalid legacy configuration: {path}") from error


def load_runtime_config(explicit_legacy_path=None, store=None, legacy_path=DEFAULT_LEGACY_PATH):
    """Explicit ``-c`` wins; otherwise new storage wins, then legacy imports."""
    if explicit_legacy_path is not None:
        config, credential = _parse_legacy(explicit_legacy_path)
        return config.legacy_runtime(credential)
    store = store or ControllerConfigurationStore(
        os.environ.get("SURRORTG_CONFIG_PATH", DEFAULT_CONFIG_PATH),
        os.environ.get("SURRORTG_CREDENTIAL_PATH", DEFAULT_SECRET_PATH),
    )
    legacy_path = os.environ.get("SURRORTG_LEGACY_CONFIG_PATH", legacy_path)
    if not store.config_path.exists():
        store.import_legacy(legacy_path)
    return store.load().legacy_runtime(store.load_secret())
