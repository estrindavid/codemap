# Builds the whole map as one JSON-ready dict: every symbol with its code, comment and connections, the tracked class's
# fields with who writes and reads each one, the entry points, and the reading order.
import ast
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from codemap.model import CLASS_KINDS, CodeModel, type_text
from codemap.order import CHAPTER_SIZE, build_chapters, find_entries, read_order_file
from codemap.source import git, is_git, list_files, module_names, read_file, read_sources

ORDER_FILES = (".codemap/order.py", "codemap_order.py")


@dataclass
class Options:
    # What to map and how: which folders, whether tests count, where runs start, which class to track, the chapter size.
    only: list[str] = field(default_factory=list)
    include_tests: bool = False
    entries: list[str] = field(default_factory=list)
    track: str | None = "auto"
    order: Path | None = None
    chapter_size: int = CHAPTER_SIZE


def is_stub(node) -> bool:
    # A body that only documents, passes or raises NotImplementedError: the real work is in the subclasses.
    body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    return all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
               or (isinstance(s, ast.Raise) and "NotImplementedError" in ast.unparse(s)) for s in body)


def symbol_json(model: CodeModel, qual: str, highlighted: dict[str, list[str]]) -> dict:
    symbol = model.symbols[qual]
    source = symbol.source
    lines = highlighted[source.path]
    start, end = symbol.line, symbol.node.end_lineno
    if symbol.kind == "constant":
        above = source.comment_above(symbol.line)
        start -= len(above)
        comment = above + source.trailing(symbol.node)
    else:
        comment = source.doc_comment(symbol.node)
        start -= len(source.comment_above(symbol.line))   # show the comment above it with the code
    if symbol.kind in CLASS_KINDS and symbol.methods:   # a class's own lines, up to its first method: each method has its box
        first = min(model.symbols[m].line - len(source.comment_above(model.symbols[m].line)) for m in symbol.methods)
        end = max(start, first - 1)
        while end > start and not source.lines[end - 1].strip():
            end -= 1
    data = {
        "qual": qual, "id": model.short(qual), "kind": symbol.kind, "subkind": symbol.subkind, "name": symbol.name,
        "module": model.short(symbol.module), "file": source.path, "line": start, "end_line": end,
        "owner": model.short(symbol.owner) if symbol.owner else None, "comment": comment,
        "code": [{"n": n, "html": lines[n - 1], "text": source.lines[n - 1]} for n in range(start, end + 1)],
    }
    if symbol.kind == "constant":
        value = symbol.node.value
        data["value"] = ast.unparse(value) if value is not None else ""
        data["uses"] = [model.short(q) for q in symbol.uses]
    if symbol.kind in CLASS_KINDS:
        data.update(bases=symbol.bases, fields=[{k: v for k, v in f.items() if k != "t"} for f in symbol.fields],
                    members=symbol.members, class_attrs=symbol.class_attrs,
                    inst_attrs=[{"name": k, "type": type_text(v)} for k, v in symbol.inst_attrs.items()],
                    methods=[model.short(m) for m in symbol.methods],
                    base_ids=[model.short(base) for base in model.base_chain(qual)[:len(symbol.node.bases)]
                              if base in model.symbols])
    if symbol.kind == "method" and symbol.owner:
        bases = [base for base in model.base_chain(symbol.owner) if base in model.symbols]
        overridden = next((f"{base}.{symbol.name}" for base in bases if f"{base}.{symbol.name}" in model.symbols), None)
        data["overrides"] = model.short(overridden) if overridden else None
    if symbol.kind == "method":
        data["stub"] = symbol.subkind == "abstract" or is_stub(symbol.node)
    if symbol.kind in ("function", "method"):
        data.update(params=[{k: v for k, v in p.items() if k != "t"} for p in symbol.params], returns=symbol.returns_text)
        events = []
        for event in symbol.events:
            item = dict(event)
            for key in ("target", "owner"):
                if item.get(key):
                    item[key] = model.short(item[key])
            events.append(item)
        data["events"] = events
    return data


def build_map(repo: Path, ref: str | None = None, options: Options | None = None) -> dict:
    # The whole map for the repo's working tree, or for a git ref.
    options = options or Options()
    repo = Path(repo).resolve()
    paths = list_files(repo, ref, options.include_tests, options.only)
    sources = read_sources(repo, ref, paths)
    model = CodeModel(sources, module_names(paths, repo.name), options.track)
    refs_by_file: dict[str, dict] = {}
    for symbol in model.symbols.values():
        for position, qual in symbol.refs.items():
            refs_by_file.setdefault(symbol.source.path, {})[position] = model.short(qual)
    highlighted = {file.path: file.highlight(refs_by_file.get(file.path, {})) for file in model.files.values()}
    symbols = {model.short(qual): symbol_json(model, qual, highlighted) for qual in model.symbols}

    for data in symbols.values():
        data["called_by"] = []
    for data in symbols.values():
        targets = [e.get("target") for e in data.get("events", []) if e["kind"] in ("call", "build")] + data.get("uses", [])
        for target in targets:
            if target in symbols and data["id"] not in symbols[target]["called_by"]:
                symbols[target]["called_by"].append(data["id"])
    implemented_by = {model.short(qual): [model.short(impl) for impl in model.implementations(qual)]
                      for qual, symbol in model.symbols.items() if symbol.kind == "method"}
    implemented_by = {key: value for key, value in implemented_by.items() if value}

    track = None
    if model.track:
        tracked = symbols[model.short(model.track)]
        fields = []
        for entry in tracked["fields"]:
            writers = [d["id"] for d in symbols.values() if any(e["kind"] == "write" and e.get("field") == entry["name"]
                                                                  for e in d.get("events", []))]
            readers = [d["id"] for d in symbols.values() if any(e["kind"] == "read" and e.get("field") == entry["name"]
                                                                  for e in d.get("events", []))]
            fields.append({**entry, "writers": writers, "readers": readers})
        track = {"id": tracked["id"], "name": tracked["name"], "fields": fields}

    entries, entry_notes = find_entries(model, read_file(repo, ref, "pyproject.toml"), symbols, options.entries)
    if not entries:   # a library: start from its public API, else from what reaches the most code
        entries, entry_notes = public_api(model, symbols)
    if not entries:
        roots = [sid for sid, d in symbols.items() if d["kind"] in ("function", "method") and not d["called_by"]]
        entries = sorted(roots, key=lambda sid: -len(symbols[sid].get("events", [])))[:5]
        entry_notes = [f"{sid} (calls the most, and nothing calls it)" for sid in entries]
    order_path, order_text = find_order_file(repo, ref, options.order)
    hand, order_error = read_order_file(order_path, order_text)
    modules = [model.short(module) for module in model.files]
    chapters, warnings = build_chapters(symbols, implemented_by, entries, modules, hand, options.chapter_size)
    warnings = model.warnings + ([order_error] if order_error else []) + warnings

    git_info = {"commit": "", "branch": "", "dirty": False}
    if is_git(repo):
        try:
            git_info["commit"] = git(repo, "rev-parse", "--short", ref or "HEAD").strip()
            git_info["branch"] = ref or git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
            if ref is None:
                git_info["dirty"] = bool(git(repo, "status", "--porcelain", "--untracked-files=no", "--", *paths[:500]).strip())
        except Exception:
            pass
    files = [file.path for file in model.files.values()]
    common = os.path.commonpath([str(PurePosixPath(path).parent) for path in files]) if files else ""
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "source": {"ref": ref or "working tree", "repo": str(repo), "name": repo.name, "common": "" if common in ("", ".") else common + "/",
                   "order_file": str(order_path) if order_path else None, **git_info},
        "modules": [{"id": model.short(module), "file": file.path, "header": file.header(), "lines": len(file.lines)}
                    for module, file in model.files.items()],
        "symbols": symbols,
        "implemented_by": implemented_by,
        "track": track,
        "entries": entries,
        "entry_notes": entry_notes,
        "chapters": chapters,
        "warnings": warnings,
        "skipped_files": len(model.warnings),
        "fingerprint": fingerprint(repo, ref, options),
    }


def public_api(model: CodeModel, symbols: dict[str, dict], limit: int = 12) -> tuple[list[str], list[str]]:
    # What the top package offers: the functions and classes its __init__.py defines or imports, functions first, in the
    # order they appear there.
    tops = sorted((module for module in model.packages if module.count(".") == 0), key=len)
    if not tops:
        return [], []
    found: list[str] = []
    for local in model.names.get(tops[0], {}):
        if local.startswith("_"):
            continue
        target = model.resolve(tops[0], local)
        if isinstance(target, str) and target in model.symbols and model.symbols[target].kind in ("function", *CLASS_KINDS):
            sid = model.short(target)
            if sid not in found:
                found.append(sid)
    found.sort(key=lambda sid: symbols[sid]["kind"] != "function")
    found = found[:limit]
    return found, [f"{sid} (part of {tops[0]}'s public API)" for sid in found[:1]] + found[1:]


def find_order_file(repo: Path, ref: str | None, asked: Path | None) -> tuple[Path | None, str | None]:
    # The hand-written order: the file asked for, else .codemap/order.py or codemap_order.py in the repo.
    if asked is not None:
        path = asked if asked.is_absolute() else (repo / asked)
        return path, path.read_text() if path.exists() else None
    for rel in ORDER_FILES:
        text = read_file(repo, ref, rel)
        if text is not None:
            return repo / rel, text
    return None, None


def fingerprint(repo: Path, ref: str | None, options: Options | None = None) -> str:
    # Changes whenever the code, pyproject.toml, the order file or the ref's commit changes: the page polls it to refresh.
    options = options or Options()
    digest = hashlib.sha1()
    extra = [repo / "pyproject.toml", *(repo / rel for rel in ORDER_FILES), *([options.order] if options.order else [])]
    if ref is None:
        paths = [repo / name for name in list_files(repo, None, options.include_tests, options.only)] + extra
        for path in paths:
            try:
                stat = Path(path).stat()
                digest.update(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode())
            except OSError:
                digest.update(f"{path}:missing".encode())
    else:
        digest.update(git(repo, "rev-parse", ref).encode())
        for path in extra:
            if Path(path).exists():
                digest.update(str(Path(path).stat().st_mtime_ns).encode())
    return digest.hexdigest()[:12]
