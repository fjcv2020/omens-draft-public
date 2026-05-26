from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

DEFAULT_API_KEY_ENV_VAR = "FAB_INSIGHTS_API_KEY"
DEFAULT_LOCAL_API_KEY_FILE = Path(".local/fab_insights_api_key.txt")
DEFAULT_ENV_FILE = Path(".env")


def _read_key_file(path: Path) -> str:
    key = path.read_text(encoding="utf-8").strip()
    if key:
        return key
    raise ValueError(f"API key file is empty: {path}")


def _read_env_file_key(path: Path, env_var: str) -> str | None:
    if not path.exists():
        return None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
            if "=" not in line:
                continue

        name, value = line.split("=", 1)
        if name.strip() != env_var:
            continue

        key = value.strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in {"'", '"'}:
            key = key[1:-1].strip()
        return key or None

    return None


def resolve_api_key(
    *,
    api_key: str | None = None,
    api_key_file: str | Path | None = None,
    default_api_key_file: str | Path | None = DEFAULT_LOCAL_API_KEY_FILE,
    env: Mapping[str, str] | None = None,
    env_var: str = DEFAULT_API_KEY_ENV_VAR,
    env_file: str | Path | None = DEFAULT_ENV_FILE,
) -> str:
    """Resolve the FAB Insights function key without embedding secrets in code."""
    if api_key is not None and api_key.strip():
        return api_key.strip()

    if api_key_file is not None:
        return _read_key_file(Path(api_key_file))

    env_map = os.environ if env is None else env
    key = env_map.get(env_var, "").strip()
    if key:
        return key

    if default_api_key_file is not None:
        path = Path(default_api_key_file)
        if path.exists():
            return _read_key_file(path)

    if env_file is not None:
        key = _read_env_file_key(Path(env_file), env_var)
        if key:
            return key

    raise ValueError(
        f"Missing FAB Insights API key. Set {env_var}, pass --api-key-file, "
        f"pass --api-key, create {DEFAULT_LOCAL_API_KEY_FILE}, or add {env_var} to {DEFAULT_ENV_FILE}."
    )
