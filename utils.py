import json
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import yaml


def save_json(filepath, data):
    """
    Save a Python object to a JSON file, pretty-printed and UTF-8 encoded.
    """
    tmpfile = filepath + ".tmp"
    with open(tmpfile, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmpfile, filepath)


def load_json(filepath):
    """
    Load a JSON file and return its contents as a Python object.
    Returns {} if file does not exist.
    """
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def now_iso():
    """
    Returns the current UTC time in ISO8601 format.
    """
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def safe_makedirs(path):
    """
    Safely creates directories (like mkdir -p). No error if already exists.
    """
    os.makedirs(path, exist_ok=True)


def truncate(s, length=100):
    """
    Truncates a string to at most 'length' characters, adding '...' if cut.
    """
    s = str(s)
    return s if len(s) <= length else s[: length - 3] + "..."


def get_config_value(config, path, default=None):
    """
    Safely get a value from a nested config dictionary using dot-notation path.
    Example: get_config_value(cfg, 'twitch.nickname', 'guest')
    """
    keys = path.split(".")
    v = config
    for k in keys:
        if isinstance(v, dict) and k in v:
            v = v[k]
        else:
            return default
    return v


# --- Verbosity handling ----------------------------------------------------

# Global verbosity level set via command line flags. 0 = quiet, 1 = verbose,
# 2 = debug.
VERBOSITY = 0


def set_verbosity(level: int) -> None:
    """Set global verbosity level."""
    global VERBOSITY
    try:
        VERBOSITY = int(level)
    except Exception:
        VERBOSITY = 0


def vprint(level: int, message: str) -> None:
    """Print ``message`` if ``VERBOSITY`` >= ``level``."""
    if VERBOSITY >= level:
        print(message)


def load_config(path: str = "config.yaml"):
    """Load YAML configuration from ``path`` or exit if missing."""
    if not os.path.exists(path):
        print("Missing config.yaml! Exiting.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_with_timeout(func, args=(), kwargs=None, timeout: int = 60):
    """Run ``func`` with ``timeout`` seconds limit in a helper thread."""
    if kwargs is None:
        kwargs = {}
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            future = executor.submit(func, *args, **kwargs)
        except RuntimeError as e:
            print(f"[ERROR][THREADPOOL] {e}")
            return False
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            print(
                f"[TIMEOUT] Function {getattr(func, '__name__', str(func))} exceeded {timeout}s"
            )
            return False
        except BaseException as e:
            print(f"[ERROR][THREAD] {e}")
            return False
