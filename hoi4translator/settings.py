"""Settings persistence and OS-backed storage of the DeepL API key."""
import getpass
import json
import os
from typing import Optional

from .config import LEGACY_SETTINGS_FILE, SETTINGS_FILE, atomic_write_text, logger

try:
    import keyring
except ImportError:
    keyring = None


KEYRING_SERVICE = "Hoi4Translator"


def _read_settings_file(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as settings_file:
            data = json.load(settings_file)
        if isinstance(data, dict):
            return data
        logger.error("Settings file must contain a JSON object: %s", path)
    except Exception as error:
        logger.error("Settings load error (%s): %s", path, error)
    return None


def _secure_keyring():
    """Use only OS-backed keyring providers, never a plaintext file backend."""
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
        backend_type = type(backend)
        module_name = backend_type.__module__.casefold()
        class_name = backend_type.__name__.casefold()
        trusted_modules = (
            "keyring.backends.windows",
            "keyring.backends.macos",
            "keyring.backends.secretservice",
            "keyring.backends.kwallet",
        )
        if not getattr(backend, "priority", 0) or not module_name.startswith(trusted_modules):
            logger.warning("No supported OS credential store is available; API key will not be persisted")
            return None
        if "plaintext" in class_name or "plaintext" in module_name:
            logger.warning("Refusing to store the API key in a plaintext keyring backend")
            return None
        return keyring
    except Exception as error:
        logger.warning("Could not access the OS credential store: %s", error)
        return None


def _keyring_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "default"


def load_api_key() -> str:
    secure_store = _secure_keyring()
    if secure_store is None:
        return ""
    try:
        return secure_store.get_password(KEYRING_SERVICE, _keyring_username()) or ""
    except Exception as error:
        logger.warning("Could not read API key from the OS credential store: %s", error)
        return ""


def store_api_key(api_key: str) -> bool:
    secure_store = _secure_keyring()
    if secure_store is None:
        return False
    username = _keyring_username()
    try:
        if api_key:
            secure_store.set_password(KEYRING_SERVICE, username, api_key)
        else:
            existing = secure_store.get_password(KEYRING_SERVICE, username)
            if existing is not None:
                secure_store.delete_password(KEYRING_SERVICE, username)
        return True
    except Exception as error:
        logger.warning("Could not update the API key in the OS credential store: %s", error)
        return False


def scrub_plaintext_api_keys() -> None:
    """Remove API keys written by older versions from legacy JSON settings."""
    paths = {SETTINGS_FILE, LEGACY_SETTINGS_FILE}
    for path in paths:
        data = _read_settings_file(path)
        if data is None or "api_key" not in data:
            continue
        data.pop("api_key", None)
        try:
            atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as error:
            logger.error("Could not remove a plaintext API key from %s: %s", path, error)


def load_settings() -> dict:
    current = _read_settings_file(SETTINGS_FILE)
    legacy = _read_settings_file(LEGACY_SETTINGS_FILE)
    settings = current if current is not None else (legacy or {})

    legacy_key = settings.pop("api_key", "")
    if not legacy_key and legacy is not None:
        legacy_key = legacy.get("api_key", "")
    if legacy_key:
        settings["_legacy_api_key"] = legacy_key

    migrated = current is None and legacy is not None
    contains_plaintext_key = bool(legacy_key)
    if migrated or contains_plaintext_key:
        save_settings(settings)
    return settings


def save_settings(data: dict) -> None:
    # The API key must only be stored in the OS credential store, never in JSON.
    safe_data = {
        name: value for name, value in data.items()
        if name not in {"api_key", "_legacy_api_key"}
    }
    try:
        atomic_write_text(
            SETTINGS_FILE, json.dumps(safe_data, ensure_ascii=False, indent=2)
        )
    except Exception as error:
        logger.error("Settings save error: %s", error)
