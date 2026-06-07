import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pymongo import MongoClient
from dotenv import load_dotenv

# Load local .env variables into system memory at the very beginning
load_dotenv()


def _detect_resources_dir() -> Path:
    """
    Implicitly finds the resources directory by checking the execution context (CWD).
    Falls back to environment variables or the file location if not found.
    """
    # 1. Honor explicit override if provided
    if env_root := os.getenv("PROJECT_ROOT"):
        return Path(env_root) / "resources"

    # 2. Implicit Discovery: Look at where the application is being executed from
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate_dir = parent / "resources"
        # Verify this directory exists and contains your master settings file
        if candidate_dir.is_dir() and (candidate_dir / "settings.yml").exists():
            return candidate_dir

    # 3. Fallback to package-relative directory if all else fails
    return Path(__file__).resolve().parent / "resources"


# Establish dynamic path routing based on runtime context
RESOURCES_DIR = _detect_resources_dir()
BASE_DIR = RESOURCES_DIR.parent
FRAMEWORK_LOGGER_NAME = "framework"
DEFAULT_FRAMEWORK_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DEFAULT_FRAMEWORK_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Regular expression pattern to match ${VAR_NAME} or ${VAR_NAME:fallback_value}
ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)(?::([^}]+))?}")


def _parse_log_level(level_name: str) -> int:
    normalized_level = str(level_name).upper()
    level = getattr(logging, normalized_level, None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid logging level configured for framework.logging.level: {level_name}")
    return level


def _as_dict(value: Any, *, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"Expected a mapping for {context}, but got {type(value).__name__}.")
    return value


def _load_and_interpolate_yaml(file_path: Path) -> dict[str, Any]:
    """Reads a configuration file as raw text, swaps environment placeholders, and parses YAML."""
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file missing at: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        raw_content = f.read()

    def match_replacer(match: re.Match[str]) -> str:
        env_var_name = match.group(1)
        fallback_value = match.group(2) or ""
        # Pull from live environment parameters; defer to fallback if empty
        return os.getenv(env_var_name, fallback_value)

    # Perform a global substitution across the text body
    interpolated_content = ENV_VAR_PATTERN.sub(match_replacer, raw_content)
    loaded = yaml.safe_load(interpolated_content)
    return _as_dict(loaded, context=str(file_path))




class ConfigSettings:
    def __init__(self):
        # 1. Bootstrap: Read the master settings.yml to find the active profile
        master_config_path = RESOURCES_DIR / "settings.yml"
        master_data = _load_and_interpolate_yaml(master_config_path)
        app_config = _as_dict(master_data.get("app"), context="settings.app")

        # Safely extract app.profile, defaulting to 'dev' if missing
        self.profile = str(app_config.get("profile", "dev"))

        # 2. Profile Loading: Read the target profile file (e.g., settings-dev.yml)
        profile_filename = f"settings-{self.profile}.yml" if self.profile != "prod" else "settings-prod.yml"
        profile_config_path = RESOURCES_DIR / profile_filename

        self._config_data = _load_and_interpolate_yaml(profile_config_path)
        mongodb_config = _as_dict(self._config_data.get("mongodb"), context=f"{profile_filename}.mongodb")

        # 3. Expose parameters to the framework
        self.mongo_uri = mongodb_config.get("uri")
        self.database_name = mongodb_config.get("database")

        if not isinstance(self.mongo_uri, str) or not self.mongo_uri or not isinstance(self.database_name, str) or not self.database_name:
            raise KeyError(f"Invalid configuration format in {profile_filename}. Missing 'mongodb.uri' or 'mongodb.database'.")

        framework_config = _as_dict(self._config_data.get("framework"), context=f"{profile_filename}.framework")
        framework_logging_config = _as_dict(framework_config.get("logging"), context=f"{profile_filename}.framework.logging")
        self.framework_config = framework_config
        self.framework_logs_enabled = bool(framework_logging_config.get("enabled", False))
        self.framework_log_level = _parse_log_level(str(framework_logging_config.get("level", "INFO")))
        self.framework_log_format = str(framework_logging_config.get("format", DEFAULT_FRAMEWORK_LOG_FORMAT))
        self.framework_log_date_format = str(framework_logging_config.get("date_format", DEFAULT_FRAMEWORK_LOG_DATE_FORMAT))


def _configure_framework_logger() -> logging.Logger:
    logger = logging.getLogger(FRAMEWORK_LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False

    if settings.framework_logs_enabled:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(settings.framework_log_format, settings.framework_log_date_format))
        logger.addHandler(handler)
        logger.setLevel(settings.framework_log_level)
    else:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)

    return logger


def get_framework_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(FRAMEWORK_LOGGER_NAME)
    return logging.getLogger(f"{FRAMEWORK_LOGGER_NAME}.{name}")


# --- SINGLETON INITIALIZATION LAYER ---
# This block triggers exactly once when the app imports this file
settings = ConfigSettings()
framework_logger = _configure_framework_logger()
framework_logger.info("Bootstrapping framework with active profile [%s]", settings.profile)
_mongo_client = MongoClient(settings.mongo_uri)
if not settings or not settings.database_name and not isinstance(settings.database_name, str):
    raise ValueError("Database name must be provided and it must be a string")
db = _mongo_client[settings.database_name]
framework_logger.debug("Mongo client initialized for database [%s]", settings.database_name)


class ConfigPropertyDescriptor:
    """Internal helper that extracts nested values from the configuration dictionary at runtime."""

    def __init__(self, path: str, default: Any = None):
        self.path = path
        self.default = default

    def __get__(self, instance: Any, owner: Any) -> Any:
        # If called from the class level (e.g., BookingRepository.mongo_db_name), return descriptor
        if instance is None:
            return self

        # Break apart dot-notation strings like "app.mongo.dbname"
        keys = self.path.split(".")
        # Pull live parsed dictionary payload from our system settings singleton
        current_node = settings._config_data

        for key in keys:
            if isinstance(current_node, dict) and key in current_node:
                current_node = current_node[key]
            else:
                return self.default
        return current_node


def value(place_holder: str) -> Any:
    """
    Spring Boot-inspired property extraction configuration tool.
    Validates formatting syntax and routes lookups directly to the settings configuration dictionary.
    """
    # 1. Enforce your validation constraint
    if place_holder.startswith("${") and not place_holder.endswith("}"):
        raise ValueError(f"Invalid place holder: {place_holder}")

    # 2. Extract key path and optional fallback default value
    if place_holder.startswith("${") and place_holder.endswith("}"):
        raw_content = place_holder[2:-1]  # Strip '${' and '}'

        # Support default fallbacks split by a colon (e.g., ${app.mongo.dbname:jpa_db})
        if ":" in raw_content:
            path, default_val = raw_content.split(":", 1)
            # Basic type-casting handling for primitives
            if default_val.isdigit():
                default_val = int(default_val)
            elif default_val.lower() == "true":
                default_val = True
            elif default_val.lower() == "false":
                default_val = False
        else:
            path = raw_content
            default_val = None

        return ConfigPropertyDescriptor(path, default_val)

    # Fallback support if they provide a raw dot-notation string without wrapper syntax
    return ConfigPropertyDescriptor(place_holder)



def get_collection(collection_name: str):
    """Provides repositories access to the shared single client pool."""
    return db[collection_name]


def close_db_connection():
    """Gracefully terminates connection pool sockets on shutdown."""
    framework_logger.debug("Closing MongoDB client connection pool")
    _mongo_client.close()
