import json
import os
from datetime import datetime

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
    return s if len(s) <= length else s[:length-3] + "..."

def get_config_value(config, path, default=None):
    """
    Safely get a value from a nested config dictionary using dot-notation path.
    Example: get_config_value(cfg, 'twitch.nickname', 'guest')
    """
    keys = path.split('.')
    v = config
    for k in keys:
        if isinstance(v, dict) and k in v:
            v = v[k]
        else:
            return default
    return v
