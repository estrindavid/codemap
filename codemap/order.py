# The reading order: where the code starts, and every symbol in the order a run reaches it. Starting from each entry point,
# the map follows calls depth first in the order they happen, so a helper lands right after the first code that calls it and
# a class right before the first code that builds or reads it. The walk is cut into chapters of about a dozen symbols. An
# order file in the repo can set the first chapters by hand; the walk fills in everything it leaves out.
import ast
import sys
import tomllib
from pathlib import Path

from codemap.model import CLASS_KINDS, IMPLICIT_METHODS, CodeModel

CHAPTER_SIZE = 12
sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))   # sizes of deep call chains
MAIN_NAMES = ("main", "cli", "run", "app", "entrypoint", "start")


# --- entry points ------------------------------------------------------------------------------------------------------------

def find_entries(model: CodeModel, pyproject: str | None, symbols: dict[str, dict], asked: list[str]) -> tuple[list[str], list[str]]:
    # The symbols a run starts from, most certain first, with where each was found: the ones asked for on the command line,
    # pyproject.toml's scripts, what `if __name__ == "__main__":` blocks call, what scripts run at their top level, then
    # functions named like main() that nothing calls.
    entries: list[str] = []
    notes: list[str] = []

    def add(qual_or_id: str, why: str) -> None:
        sid = qual_or_id if qual_or_id in symbols else model.short(qual_or_id)
        if sid in symbols and sid not in entries:
            entries.append(sid)
            notes.append(f"{sid} ({why})")

    for name in asked:
        match = [sid for sid in symbols if sid == name or sid.endswith("." + name) or symbols[sid]["qual"] == name]
        if match:
            add(match[0], "asked for")
    if pyproject:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            data = {}
        scripts = {**data.get("project", {}).get("scripts", {}), **data.get("project", {}).get("gui-scripts", {}),
                   **data.get("tool", {}).get("poetry", {}).get("scripts", {})}
        for command, target in scripts.items():
            if isinstance(target, str) and ":" in target:
                module, _, attr = target.partition(":")
                add(f"{module}.{attr.split('[')[0].strip()}", f"the `{command}` command")
    imported = imported_modules(model)
    for module, tree in model.trees.items():
        blocks = [node for node in tree.body if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test)
                  and "__main__" in ast.unparse(node.test)]
        loose = [] if module in imported else [node for node in tree.body if isinstance(node, (ast.Expr, ast.Assign))
                                               and any(isinstance(n, ast.Call) for n in ast.walk(node))]
        is_main_module = module.endswith("__main__") or module == "__main__"
        statements = [child for block in blocks for child in block.body] + loose
        if is_main_module:
            statements = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        if not statements:
            continue
        for event in model.scan_statements(module, statements):
            if event["kind"] in ("call", "build") and event.get("target"):
                add(event["target"], f"run by {model.files[module].path}")
    named = [sid for sid, data in symbols.items() if data["kind"] == "function" and not data.get("called_by")
             and (data["name"] in MAIN_NAMES or data["name"].endswith("_main"))]
    for sid in sorted(named, key=lambda sid: (symbols[sid]["name"] not in MAIN_NAMES, symbols[sid]["name"].count("_"), sid)):
        add(sid, "named like an entry point")
    return entries, notes


def imported_modules(model: CodeModel) -> set[str]:
    # The modules some other module of the repo imports.
    out = set()
    for module, names in model.names.items():
        for target in names.values():
            if isinstance(target, tuple) and target[0] == "module":
                out.add(target[1])
            elif isinstance(target, str) and not target.startswith("ext:"):
                out.add(target.rpartition(".")[0])
    for module in list(out):
        parts = module.split(".")
        out.update(".".join(parts[:i]) for i in range(1, len(parts)))
    return out


# --- the walk ---------------------------------------------------------------------------------------------------------------

def next_steps(sid: str, symbols: dict[str, dict], implemented_by: dict[str, list[str]], rank) -> list[str]:
    # What a symbol leads to, in the order it happens: for a function, what it calls, builds, reads and raises (a call to a
    # method that only subclasses fill in goes on to each subclass's version, in the order their classes were reached); for
    # a class, its bases and the methods that run when it's built.
    data = symbols[sid]
    out: list[str] = []

    def add(target) -> None:
        if target in symbols and target != sid and target not in out:
            out.append(target)

    if data["kind"] in CLASS_KINDS:
        for base in data.get("base_ids", []):
            add(base)
        for method in data.get("methods", []):
            name = symbols[method]["name"] if method in symbols else ""
            if name in IMPLICIT_METHODS or symbols.get(method, {}).get("subkind") == "validator":
                add(method)
        return out
    for used in data.get("uses", []):
        add(used)
    for event in data.get("events", []):
        target = event.get("target") if event["kind"] in ("call", "build", "const", "raise") else event.get("owner")
        if event["kind"] not in ("call", "build", "const", "raise", "field", "enum"):
            continue
        add(target)
        if event["kind"] == "call" and symbols.get(target, {}).get("stub"):
            for impl in sorted(implemented_by.get(target, []), key=lambda m: rank(symbols[m].get("owner"))):
                add(impl)
    return out


class Walk:
    # The depth-first walk: the order symbols are first reached, and which symbol reached each one.

    def __init__(self, symbols: dict[str, dict], implemented_by: dict[str, list[str]], placed: set[str]) -> None:
        self.symbols = symbols
        self.implemented_by = implemented_by
        self.placed = placed
        self.children: dict[str, list[str]] = {}
        self.rank: dict[str, int] = {sid: index for index, sid in enumerate(placed)}

    def visit(self, sid: str) -> list[str]:
        # Places sid (a method's class first), then everything it leads to that isn't placed yet. Returns the symbols placed
        # at the top of the walk: sid, after its class when that wasn't placed yet.
        roots: list[str] = []
        stack: list[tuple[str, str | None]] = [(sid, None)]
        while stack:
            current, parent = stack.pop()
            if current in self.placed:
                continue
            owner = self.symbols[current].get("owner")
            if owner in self.symbols and owner not in self.placed:
                stack.append((current, parent))
                stack.append((owner, parent))
                continue
            self.placed.add(current)
            self.rank[current] = len(self.rank)
            self.children.setdefault(current, [])
            if parent is None:
                roots.append(current)
            else:
                self.children[parent].append(current)
            for step in reversed(next_steps(current, self.symbols, self.implemented_by, self.position)):
                if step not in self.placed:
                    stack.append((step, current))
        return roots

    def position(self, sid: str | None) -> int:
        # When a symbol was reached; symbols not reached yet sort last.
        return self.rank.get(sid, len(self.symbols)) if sid else len(self.symbols)

    def size(self, sid: str, memo: dict) -> int:
        if sid not in memo:
            memo[sid] = 1 + sum(self.size(child, memo) for child in self.children.get(sid, []))
        return memo[sid]

    def preorder(self, sid: str) -> list[str]:
        out, stack = [], [sid]
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(reversed(self.children.get(current, [])))
        return out

    def carve(self, root: str, limit: int) -> list[tuple[str, list[str], bool]]:
        # Cuts root's part of the walk into chapters of at most `limit` symbols, keeping the walk's order: a small branch
        # joins the chapter being filled, a big one gets chapters of its own, and a few symbols waiting in front of a big
        # branch open its first chapter instead of making one of their own. Returns (the symbol a chapter is named after,
        # its symbols, whether it continues an earlier chapter).
        memo: dict[str, int] = {}
        chapters: list[tuple[str, list[str], bool]] = []

        def cut(node: str, prefix: list[str], head: str, continued: bool) -> None:
            if len(prefix) + self.size(node, memo) <= limit:
                chapters.append((head, prefix + self.preorder(node), continued))
                return
            current = prefix + [node]
            for child in self.children.get(node, []):
                size = self.size(child, memo)
                if size > limit:
                    if len(current) <= 3:
                        cut(child, current, head, continued)
                    else:
                        chapters.append((head, current, continued))
                        cut(child, [], child, False)
                    current, head, continued = [], node, True
                elif len(current) + size <= limit:
                    current += self.preorder(child)
                else:
                    chapters.append((head, current, continued))
                    current, head, continued = self.preorder(child), node, True
            if current:
                chapters.append((head, current, continued))

        cut(root, [], root, False)
        merged: list[tuple[str, list[str], bool]] = []
        for head, items, continued in chapters:   # a leftover of one or two symbols joins the chapter before it
            if merged and len(items) <= 2 and len(merged[-1][1]) + len(items) <= limit + 2:
                merged[-1] = (merged[-1][0], merged[-1][1] + items, merged[-1][2])
            else:
                merged.append((head, items, continued))
        return merged


# --- chapters -------------------------------------------------------------------------------------------------------------

def display(symbols: dict[str, dict], sid: str) -> str:
    data = symbols[sid]
    owner = data.get("owner")
    return f"{symbols[owner]['name']}.{data['name']}" if owner in symbols else data["name"]


def first_sentence(symbols: dict[str, dict], sid: str) -> str:
    comment = " ".join(symbols[sid].get("comment") or [])
    return comment.split(". ")[0].rstrip(".") + "." if comment else ""


def chapter_from(symbols: dict[str, dict], head: str, items: list[str], continued: bool, section: str) -> dict:
    # A chapter named after its first symbol; its description says what that symbol does, and which one it continues.
    files = list(dict.fromkeys(symbols[sid]["file"] for sid in items))
    goal = first_sentence(symbols, items[0])
    if continued or head != items[0]:
        goal = f"Continues {display(symbols, head)}. " + goal
    return {"id": "", "title": display(symbols, items[0]), "files": ", ".join(files[:3]) + (f" +{len(files) - 3}" if len(files) > 3 else ""),
            "goal": goal.strip(), "discuss": [], "section": section, "items": [{"ref": sid, "note": ""} for sid in items]}


def auto_chapters(symbols: dict[str, dict], implemented_by: dict[str, list[str]], entries: list[str], placed: set[str],
                  modules: list[str], limit: int = CHAPTER_SIZE) -> list[dict]:
    # Chapters for everything not placed yet: the walk from each entry point, then, module by module, the code no entry
    # point reaches, starting from what nothing else calls.
    walk = Walk(symbols, implemented_by, placed)
    chapters = []
    for entry in entries:
        if entry in placed:
            continue
        for root in walk.visit(entry):
            chapters += [chapter_from(symbols, head, items, again, "entry") for head, items, again in walk.carve(root, limit)]
    by_module: dict[str, list[str]] = {}
    for sid, data in symbols.items():
        by_module.setdefault(data["module"], []).append(sid)
    order = {module: index for index, module in enumerate(modules)}
    for module in sorted(by_module, key=lambda m: order.get(m, len(order))):
        rest = sorted((sid for sid in by_module[module] if sid not in placed), key=lambda sid: symbols[sid]["line"])
        if not rest:
            continue
        roots = [sid for sid in rest if not any(caller not in placed and caller != sid
                                                 for caller in symbols[sid].get("called_by", []))] or rest
        pieces: list[str] = []
        for root in [*roots, *rest]:
            if root in placed:
                continue
            for placed_root in walk.visit(root):
                pieces += walk.preorder(placed_root)
        for start in range(0, len(pieces), limit):
            items = pieces[start:start + limit]
            chapter = chapter_from(symbols, items[0], items, False, "rest")
            chapter["title"] = f"Also in {module}" + (" (continued)" if start > 0 else "")
            chapter["goal"] = "Code no entry point reaches: helpers, public API for other code to call, or code nothing uses."
            chapters.append(chapter)
    return chapters


def read_order_file(path: Path | None, text: str | None) -> tuple[list[dict] | None, str | None]:
    # CHAPTERS from an order file: [{"title": ..., "goal": ..., "items": [(id, note) or (id, note, options) or id]}].
    if text is None:
        return None, None
    namespace: dict = {}
    try:
        exec(compile(text, str(path or "order.py"), "exec"), namespace)
        return list(namespace["CHAPTERS"]), None
    except Exception as exc:   # a typo in the order file shouldn't take the map down
        return None, f"the order file could not be read: {type(exc).__name__}: {exc}"


def build_chapters(symbols: dict[str, dict], implemented_by: dict[str, list[str]], entries: list[str], modules: list[str],
                   hand: list[dict] | None, limit: int = CHAPTER_SIZE) -> tuple[list[dict], list[str]]:
    # The hand-written chapters first (every id checked against the code), then the walk for everything they leave out.
    warnings: list[str] = []
    placed: set[str] = set()
    chapters: list[dict] = []
    for number, chapter in enumerate(hand or [], start=1):
        items = []
        for entry in chapter.get("items", []):
            ref, note, *extra = entry if isinstance(entry, (tuple, list)) else (entry, "")
            options = extra[0] if extra else {}
            item = {"ref": ref, "note": note, **options}
            if ref.startswith("group:"):
                item["members"] = [member for member in options.get("members", []) if member in symbols]
                placed.update(item["members"])
                for member in options.get("members", []):
                    if member not in symbols:
                        warnings.append(f"chapter {number}: {member} (in {ref}) is not in the code any more")
            elif ref not in symbols:
                item["stale"] = True
                warnings.append(f"chapter {number}: {ref} is not in the code any more")
            else:
                placed.add(ref)
            items.append(item)
        chapters.append({"id": chapter.get("id", ""), "title": chapter.get("title", f"Chapter {number}"),
                         "files": chapter.get("files", ""), "goal": chapter.get("goal", ""),
                         "discuss": chapter.get("discuss", []), "section": "hand", "items": items})
    chapters += auto_chapters(symbols, implemented_by, entries, placed, modules, limit)
    used = set()
    for number, chapter in enumerate(chapters, start=1):
        chapter["number"] = number
        base = chapter["id"] or f"c{number}"
        chapter["id"] = base if base not in used else f"{base}-{number}"
        used.add(chapter["id"])
    return chapters, warnings

