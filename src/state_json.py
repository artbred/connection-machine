"""Publish a complete local state snapshot without buffering its JSON encoding."""

import json
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import mkstemp


def write_state_json(path: Path, state: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
        try:
            os.chmod(tmp, S_IMODE(path.stat().st_mode))
        except FileNotFoundError:
            pass  # New personal state files keep mkstemp's private permissions.
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
