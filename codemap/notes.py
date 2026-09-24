# Your notes and read marks, one JSON file per repo, kept outside the repo in ~/.codemap so they never show up in git. Each
# is saved under the id of the thing it's about (records.Graph.node); when code moves to another module or class, the notes
# follow it on the next build, after a backup of the file.
import hashlib
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

LOCK = threading.Lock()


def default_path(checkout: Path) -> Path:
    # ~/.codemap/<repo name>-<short hash of its path>/notes.json, shared by every worktree of the repo.
    slug = f"{checkout.name}-{hashlib.sha1(str(checkout.resolve()).encode()).hexdigest()[:8]}"
    return Path.home() / ".codemap" / slug / "notes.json"


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": {}}


def save(path: Path, notes: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(notes, indent=1, sort_keys=True))
    temporary.replace(path)


def update(path: Path, item_id: str, change: dict) -> dict:
    # Sets the note and/or the read mark of one item.
    with LOCK:
        notes = load(path)
        entry = notes["items"].setdefault(item_id, {})
        for key in ("note", "read"):
            if key in change:
                entry[key] = change[key]
        entry["updated"] = datetime.now().isoformat(timespec="seconds")
        save(path, notes)
        return entry


def tail(symbol: dict) -> str:
    # An id without its module: records.Graph.node -> Graph.node.
    return symbol["id"][len(symbol["module"]) + 1:] if symbol["id"].startswith(symbol["module"] + ".") else symbol["id"]


def moved_to(old_id: str, symbols: dict[str, dict], by_tail: dict[str, list[str]]) -> str | None:
    # Where a symbol that's gone went: the one symbol whose id ends the same way, trying the longest ending first, so
    # models.Graph.node finds records.Graph.node. None when there's no such symbol, or more than one.
    parts = old_id.split(".")
    for start in range(1, len(parts)):
        matches = by_tail.get(".".join(parts[start:]), [])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
    return None


def follow_moves(path: Path, symbols: dict[str, dict]) -> tuple[list[tuple[str, str]], list[str]]:
    # Moves the notes of symbols that were renamed or moved to their new ids. Returns the moves made and the ids whose code
    # is gone for good. Call it only with a complete map: a file that doesn't parse would look like deleted code.
    with LOCK:
        notes = load(path)
        by_tail: dict[str, list[str]] = {}
        for symbol in symbols.values():
            by_tail.setdefault(tail(symbol), []).append(symbol["id"])
        moves, gone = [], []
        for key in list(notes["items"]):
            base, sep, part = key.partition("#")
            if base in symbols or ":" in base:
                continue
            target = moved_to(base, symbols, by_tail)
            if target is None:
                gone.append(key)
                continue
            moves.append((key, target + sep + part))
        if moves:
            shutil.copy2(path, path.with_name(f"notes.backup-{datetime.now():%Y%m%d-%H%M%S}.json"))
            for old, new in moves:
                entry = notes["items"].pop(old)
                current = notes["items"].setdefault(new, {})
                current["read"] = bool(current.get("read") or entry.get("read"))
                if not current.get("note") and entry.get("note"):
                    current["note"] = entry["note"]
                current["updated"] = max(current.get("updated", ""), entry.get("updated", ""))
            save(path, notes)
        return moves, gone
