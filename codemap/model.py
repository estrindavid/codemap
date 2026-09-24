# What the map knows about the code: every module-level class, function and constant, each class's methods, and for every
# function what it does in the order it runs (calls, classes it builds, fields it reads, constants it uses, what it raises).
# Types are followed just far enough to tell which method a call lands on: annotations, constructors, return types.
import ast
from types import SimpleNamespace

from codemap.source import SourceFile, first_line

RECORD_BASES = {"BaseModel", "BaseSettings", "NamedTuple", "TypedDict", "Struct"}
RECORD_DECORATORS = {"dataclass", "define", "frozen", "mutable", "attrs", "s", "dataclass_json"}
ENUM_BASES = {"Enum", "StrEnum", "IntEnum", "Flag", "IntFlag", "ReprEnum"}
ERROR_BASES = {"Exception", "BaseException", "RuntimeError", "ValueError", "KeyError", "TypeError", "LookupError",
               "OSError", "IOError", "AttributeError", "IndexError", "NotImplementedError", "ArithmeticError",
               "PermissionError", "FileNotFoundError", "TimeoutError", "ConnectionError", "Warning", "UserWarning"}
CLASS_KINDS = ("record", "enum", "class", "error")
IMPLICIT_METHODS = ("__init__", "__post_init__", "__new__")
UNKNOWN = None


# --- types, just enough to follow data through the code ------------------------------------------------------------------
# A type is None (unknown), a string (a class's qualified name, or a builtin such as "str"), or a tuple:
# ("list", T), ("set", T), ("dict", K, V), ("tuple", [T, ...]), ("union", [T, ...]), ("type", T), ("classref", qual),
# ("funcref", qual), ("const", qual), ("bound", qual), ("module", name), ("container_method", T, name).

def strip_none(t):
    # Optional[T] -> T, so an optional field still resolves.
    if isinstance(t, tuple) and t[0] == "union":
        members = [member for member in t[1] if member not in ("None", None)]
        return members[0] if len(members) == 1 else ("union", members)
    return t


def type_text(t) -> str:
    # A readable form of a type, for the map.
    if t is None:
        return "?"
    if isinstance(t, str):
        return t.rsplit(".", 1)[-1]
    if t[0] in ("list", "set"):
        return f"{t[0]}[{type_text(t[1])}]"
    if t[0] == "dict":
        return f"dict[{type_text(t[1])}, {type_text(t[2])}]"
    if t[0] == "tuple":
        return "tuple[" + ", ".join(type_text(x) for x in t[1]) + "]"
    if t[0] == "union":
        return " | ".join(type_text(x) for x in t[1])
    return t[0]


def elem_of(t):
    # One item's type when iterating: lists and sets give their item, dicts their key.
    t = strip_none(t)
    if isinstance(t, tuple) and t[0] in ("list", "set", "dict"):
        return t[1]
    if t == "str":
        return "str"
    return UNKNOWN


# --- symbols -------------------------------------------------------------------------------------------------------------------

class Symbol:
    # One thing the map can show: a module constant, a class, a function or a method.

    def __init__(self, qual: str, kind: str, name: str, module: str, node, source: SourceFile, owner: str | None = None) -> None:
        self.qual = qual
        self.kind = kind                  # constant | record | enum | class | error | function | method
        self.name = name
        self.module = module
        self.node = node
        self.source = source
        self.owner = owner                # the class, for methods
        self.subkind = ""                 # methods: static, classmethod, property, cached property, validator, abstract, async
        self.bases: list[str] = []
        self.fields: list[dict] = []      # records: name, type, default, comment
        self.members: list[dict] = []     # enums: name, value, comment
        self.class_attrs: list[dict] = [] # other classes: name, value, comment
        self.inst_attrs: dict[str, object] = {}   # self.x -> type, from __init__
        self.methods: list[str] = []
        self.params: list[dict] = []
        self.returns = UNKNOWN
        self.returns_text = ""
        self.events: list[dict] = []      # what a function does, in order: call, build, read, write, field, attr, const, enum,
                                          # raise, external
        self.uses: list[str] = []         # constants: the classes, functions and constants their value names
        self.refs: dict[tuple[int, int], str] = {}

    @property
    def line(self) -> int:
        return first_line(self.node)


class CodeModel:
    # Everything the map knows about the code: symbols by qualified name, and what each name means in each module.

    def __init__(self, sources: dict[str, str], modules: dict[str, str], track: str | None = "auto") -> None:
        # sources: path -> text; modules: path -> module name. A file that doesn't parse is skipped and named in warnings.
        self.warnings: list[str] = []
        self.files: dict[str, SourceFile] = {}
        self.trees: dict[str, ast.Module] = {}
        self.packages: set[str] = set()
        for path, text in sources.items():
            module = modules[path]
            try:
                tree = ast.parse(text, filename=path)
            except (SyntaxError, ValueError) as exc:
                line = f":{exc.lineno}" if getattr(exc, "lineno", None) else ""
                self.warnings.append(f"skipped {path}{line}: it doesn't parse ({getattr(exc, 'msg', exc)})")
                continue
            self.files[module] = SourceFile(path, text)
            self.trees[module] = tree
            if path.endswith("__init__.py"):
                self.packages.add(module)
        self.module_names = set(self.files)
        self.module_prefixes = {".".join(module.split(".")[:i]) for module in self.module_names
                                for i in range(1, module.count(".") + 2)}
        self._chains: dict[str, list[str]] = {}
        self._subclasses: dict[str, list[str]] | None = None
        self._const_types: dict[str, object] = {}
        self.prefix = self._common_prefix()
        self.symbols: dict[str, Symbol] = {}
        self.names: dict[str, dict[str, object]] = {}
        self._collect()
        self._imports()
        self._classify()
        self._members()
        self._constant_uses()
        self.track = None
        self._scan()
        if track == "auto":
            self.track = self._choose_track()
        elif track:
            self.track = self._find_class(track)
            if self.track is None:
                self.warnings.append(f"--track {track}: no class by that name")
        if self.track:
            self._scan()

    # -- names -------------------------------------------------------------------------------------------------------------

    def _common_prefix(self) -> str:
        # "pkg." when every module sits inside one top-level package, so the map can say records.Node for pkg.records.Node.
        tops = {module.split(".")[0] for module in self.module_names}
        if len(tops) == 1 and len(self.module_names) > 1:
            top = next(iter(tops))
            if top in self.packages or any(module.startswith(top + ".") for module in self.module_names):
                return top + "."
        return ""

    def short(self, qual: str) -> str:
        return qual[len(self.prefix):] if self.prefix and qual.startswith(self.prefix) else qual

    def is_module(self, name: str) -> bool:
        # A module of the repo, or a folder of them (a namespace package).
        return name in self.module_prefixes

    def _find_class(self, name: str) -> str | None:
        for qual, symbol in self.symbols.items():
            if symbol.kind in CLASS_KINDS and (qual == name or self.short(qual) == name or symbol.name == name):
                return qual
        return None

    # -- collection --------------------------------------------------------------------------------------------------------

    def _collect(self) -> None:
        # Module-level classes, functions and constants, and each class's methods.
        for module, tree in self.trees.items():
            source = self.files[module]
            self.names[module] = {}
            for node in self._top_level(tree.body):
                if isinstance(node, ast.ClassDef):
                    qual = f"{module}.{node.name}"
                    symbol = Symbol(qual, "class", node.name, module, node, source)
                    self.symbols[qual] = symbol
                    self.names[module][node.name] = qual
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            method = Symbol(f"{qual}.{item.name}", "method", item.name, module, item, source, owner=qual)
                            method.subkind = self._subkind(item)
                            self.symbols[method.qual] = method
                            if method.qual not in symbol.methods:
                                symbol.methods.append(method.qual)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{module}.{node.name}"
                    function = Symbol(qual, "function", node.name, module, node, source)
                    function.subkind = "async" if isinstance(node, ast.AsyncFunctionDef) else ""
                    self.symbols[qual] = function
                    self.names[module][node.name] = qual
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id != "__all__":
                            qual = f"{module}.{target.id}"
                            if qual not in self.symbols:
                                self.symbols[qual] = Symbol(qual, "constant", target.id, module, node, source)
                            self.names[module][target.id] = qual

    @staticmethod
    def _top_level(body: list) -> list:
        # A module's statements, looking inside top-level if/try/with blocks (TYPE_CHECKING imports, optional imports).
        out = []
        for node in body:
            if isinstance(node, (ast.If, ast.Try, ast.With)) and not (
                    isinstance(node, ast.If) and "__name__" in ast.unparse(node.test)):
                out += CodeModel._top_level([*node.body, *getattr(node, "orelse", []), *getattr(node, "finalbody", []),
                                             *[child for handler in getattr(node, "handlers", []) for child in handler.body]])
            else:
                out.append(node)
        return out

    @staticmethod
    def _subkind(node) -> str:
        names = {ast.unparse(d).split("(")[0].rsplit(".", 1)[-1] for d in node.decorator_list}
        for decorator, label in (("staticmethod", "static"), ("classmethod", "classmethod"), ("property", "property"),
                                 ("cached_property", "cached property"), ("model_validator", "validator"),
                                 ("field_validator", "validator"), ("validator", "validator"), ("root_validator", "validator"),
                                 ("abstractmethod", "abstract")):
            if decorator in names:
                return label
        if any(name.endswith(("setter", "deleter")) for name in names):
            return "property"
        return "async" if isinstance(node, ast.AsyncFunctionDef) else ""

    def _imports(self) -> None:
        # What each imported name means in each module: a symbol of the repo, a module ("module", name), or "ext:..." for
        # anything from outside the repo.
        for module, tree in self.trees.items():
            package = module if module in self.packages else module.rpartition(".")[0]
            for node in self._top_level(tree.body):
                if isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        parts = package.split(".") if package else []
                        parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                        base = ".".join([*parts, *([node.module] if node.module else [])])
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        local = alias.asname or alias.name
                        full = f"{base}.{alias.name}" if base else alias.name
                        if self.is_module(full):
                            self.names[module][local] = ("module", full)
                        elif self.is_module(base):
                            self.names[module][local] = full
                        else:
                            self.names[module][local] = f"ext:{full}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname:
                            self.names[module][alias.asname] = ("module", alias.name)
                        else:
                            top = alias.name.split(".")[0]
                            self.names[module][top] = ("module", top)

    def resolve(self, module: str, name: str, depth: int = 0):
        # A name as seen from one module: a symbol's qual, ("module", name), "ext:..." or None. Follows re-exports, such as a
        # package's __init__.py importing a class from one of its modules.
        target = self.names.get(module, {}).get(name)
        if isinstance(target, str) and not target.startswith("ext:") and target not in self.symbols and depth < 6:
            owner, _, attr = target.rpartition(".")
            if owner in self.names:
                again = self.resolve(owner, attr, depth + 1)
                if again is not None:
                    return again
        return target

    def member_of_module(self, module: str, attr: str):
        # module.attr: a symbol, a submodule, or None.
        if module in self.names:
            found = self.resolve(module, attr)
            if isinstance(found, str) and found in self.symbols:
                return found
            if isinstance(found, tuple):
                return found
        full = f"{module}.{attr}"
        if full in self.symbols:
            return full
        if self.is_module(full):
            return ("module", full)
        return None

    # -- classes -----------------------------------------------------------------------------------------------------------

    def base_chain(self, qual: str) -> list[str]:
        # The names of a class's bases, all the way up; the repo's own classes by qual, others by their plain name.
        if qual not in self._chains:
            self._chains[qual] = self._chain(qual, set())
        return self._chains[qual]

    def _chain(self, qual: str, seen: set) -> list[str]:
        symbol = self.symbols.get(qual)
        if symbol is None or qual in seen or symbol.kind not in CLASS_KINDS:
            return []
        seen.add(qual)
        chain = []
        for base in symbol.node.bases:
            target = self.expr_target(symbol.module, base)
            if isinstance(target, str) and target in self.symbols and target != qual:
                chain.append(target)
                chain += self._chain(target, seen)
            else:
                chain.append(ast.unparse(base).split("[")[0].rsplit(".", 1)[-1])
        return chain

    def expr_target(self, module: str, node):
        # What a Name or dotted Attribute refers to, statically: a symbol's qual, ("module", name), or None.
        if isinstance(node, ast.Name):
            return self.resolve(module, node.id)
        if isinstance(node, ast.Attribute):
            base = self.expr_target(module, node.value)
            if isinstance(base, tuple) and base[0] == "module":
                return self.member_of_module(base[1], node.attr)
        if isinstance(node, ast.Subscript):
            return self.expr_target(module, node.value)
        return None

    def _classify(self) -> None:
        # record, enum, error or plain class, from the bases and decorators.
        for symbol in list(self.symbols.values()):
            if symbol.kind != "class":
                continue
            chain = self.base_chain(symbol.qual)
            symbol.bases = [self.short(base) for base in chain[:len(symbol.node.bases)]]
            names = {base.rsplit(".", 1)[-1] for base in chain}
            decorators = {ast.unparse(d).split("(")[0].rsplit(".", 1)[-1] for d in symbol.node.decorator_list}
            if names & ENUM_BASES:
                symbol.kind = "enum"
            elif names & RECORD_BASES or decorators & RECORD_DECORATORS:
                symbol.kind = "record"
            elif names & ERROR_BASES or any(name.endswith(("Error", "Exception")) for name in names):
                symbol.kind = "error"
        # a class based on a record is a record too, once its base is known
        for _ in range(3):
            for symbol in self.symbols.values():
                if symbol.kind == "class" and any(self.symbols.get(base, SimpleNamespace(kind="")).kind == "record"
                                                  for base in self.base_chain(symbol.qual)):
                    symbol.kind = "record"

    def annotation(self, node, module: str, owner: str | None = None):
        # An annotation as a type.
        if node is None:
            return UNKNOWN
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return self.annotation(ast.parse(node.value, mode="eval").body, module, owner)
            except SyntaxError:
                return UNKNOWN
        if isinstance(node, ast.Constant) and node.value is None:
            return "None"
        if isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node, ast.Name) and node.id == "Self":
                return owner
            target = self.expr_target(module, node)
            if isinstance(target, str) and target in self.symbols:
                return target
            return ast.unparse(node).rsplit(".", 1)[-1]
        if isinstance(node, ast.Subscript):
            base = ast.unparse(node.value).rsplit(".", 1)[-1]
            args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            types = [self.annotation(arg, module, owner) for arg in args]
            if base in ("list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "Generator", "Collection",
                        "deque", "Deque", "AsyncIterator", "AsyncIterable"):
                return ("list", types[0])
            if base in ("set", "frozenset", "Set", "FrozenSet", "AbstractSet", "MutableSet"):
                return ("set", types[0])
            if base in ("dict", "Dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict") and len(types) == 2:
                return ("dict", types[0], types[1])
            if base in ("tuple", "Tuple"):
                return ("tuple", types)
            if base in ("ClassVar", "Optional", "Final", "Annotated", "Required", "NotRequired", "ReadOnly"):
                return ("union", [types[0], "None"]) if base == "Optional" else types[0]
            if base == "Union":
                return ("union", types)
            if base in ("type", "Type"):
                return ("type", types[0])
            return UNKNOWN
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left, right = self.annotation(node.left, module, owner), self.annotation(node.right, module, owner)
            members = []
            for side in (left, right):
                members += side[1] if isinstance(side, tuple) and side[0] == "union" else [side]
            return ("union", members)
        return UNKNOWN

    def _members(self) -> None:
        # Fields, enum members, class attributes, parameters, return types and instance attributes.
        for symbol in self.symbols.values():
            if symbol.kind in ("function", "method"):
                self._signature(symbol)
        for symbol in self.symbols.values():
            if symbol.kind not in CLASS_KINDS:
                continue
            for item in symbol.node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = item.target.id
                    text = ast.unparse(item.annotation)
                    entry = {"name": name, "type": text, "default": ast.unparse(item.value) if item.value else None,
                             "comment": " ".join(symbol.source.trailing(item)), "line": item.lineno,
                             "above": " ".join(symbol.source.comment_above(item.lineno))}
                    if name == "model_config" or text.startswith(("ClassVar", "typing.ClassVar")):
                        symbol.class_attrs.append({"name": name, "value": entry["default"] or text, "comment": entry["comment"]})
                    elif symbol.kind in ("record", "class", "error"):
                        entry["t"] = self.annotation(item.annotation, symbol.module, symbol.qual)
                        symbol.fields.append(entry)
                    else:
                        symbol.class_attrs.append({"name": name, "value": entry["default"] or text, "comment": entry["comment"]})
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            value = ast.unparse(item.value)
                            comment = " ".join(symbol.source.trailing(item))
                            if symbol.kind == "enum":
                                literal = item.value.value if isinstance(item.value, ast.Constant) else value
                                symbol.members.append({"name": target.id, "value": literal, "comment": comment})
                            else:
                                symbol.class_attrs.append({"name": target.id, "value": value, "comment": comment})
        for symbol in self.symbols.values():
            if symbol.kind in CLASS_KINDS:
                self._instance_attrs(symbol)

    def _signature(self, symbol: Symbol) -> None:
        args = symbol.node.args
        params = []
        for arg in [*args.posonlyargs, *args.args]:
            params.append({"name": arg.arg, "type": ast.unparse(arg.annotation) if arg.annotation else "",
                           "t": self.annotation(arg.annotation, symbol.module, symbol.owner)})
        if args.vararg:
            params.append({"name": "*" + args.vararg.arg, "type": ast.unparse(args.vararg.annotation) if args.vararg.annotation else "",
                           "t": ("list", self.annotation(args.vararg.annotation, symbol.module, symbol.owner))})
        for arg in args.kwonlyargs:
            params.append({"name": arg.arg, "type": ast.unparse(arg.annotation) if arg.annotation else "",
                           "t": self.annotation(arg.annotation, symbol.module, symbol.owner), "kwonly": True})
        if args.kwarg:
            params.append({"name": "**" + args.kwarg.arg, "type": "", "t": UNKNOWN})
        symbol.params = params
        symbol.returns = self.annotation(symbol.node.returns, symbol.module, symbol.owner)
        symbol.returns_text = ast.unparse(symbol.node.returns) if symbol.node.returns else ""

    def _instance_attrs(self, symbol: Symbol) -> None:
        # self.x = ... in __init__ (and in the bases' __init__), with the type when it can be told.
        for base in self.base_chain(symbol.qual)[::-1]:
            if base in self.symbols:
                symbol.inst_attrs.update(self.symbols[base].inst_attrs)
        init = self.symbols.get(f"{symbol.qual}.__init__")
        if init is None:
            return
        env = {param["name"]: param["t"] for param in init.params}
        env["self"] = symbol.qual
        for node in ast.walk(init.node):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                    if isinstance(node, ast.AnnAssign):
                        t = self.annotation(node.annotation, symbol.module, symbol.qual)
                    else:
                        t = Typer(self, symbol.module, symbol.qual).type_of(node.value, env)
                    symbol.inst_attrs.setdefault(target.attr, t)

    def _constant_uses(self) -> None:
        # The symbols each constant's value names (REGISTRY = {"a": StepA}, STEPS = [Tax()]), linked in its source.
        for symbol in self.symbols.values():
            if symbol.kind != "constant" or symbol.node.value is None:
                continue
            for node in ast.walk(symbol.node.value):
                if not isinstance(node, (ast.Name, ast.Attribute)):
                    continue
                target = self.expr_target(symbol.module, node)
                if isinstance(target, str) and target in self.symbols and target != symbol.qual:
                    if target not in symbol.uses:
                        symbol.uses.append(target)
                    if isinstance(node, ast.Name):
                        symbol.refs[(node.lineno, symbol.source.char_col(node.lineno, node.col_offset))] = target
                    else:
                        col = symbol.source.char_col(node.end_lineno, node.end_col_offset) - len(node.attr)
                        symbol.refs[(node.end_lineno, col)] = target

    def const_type(self, qual: str):
        # A constant's type: its annotation, else the type of its value.
        if qual not in self._const_types:
            self._const_types[qual] = UNKNOWN   # a constant defined from itself stays unknown
            symbol = self.symbols[qual]
            node = symbol.node
            if isinstance(node, ast.AnnAssign):
                self._const_types[qual] = self.annotation(node.annotation, symbol.module)
            elif node.value is not None:
                self._const_types[qual] = Typer(self, symbol.module, None).type_of(node.value, {})
        return self._const_types[qual]

    def class_member(self, class_qual: str, name: str):
        # ("field", type) / ("method", qual) / ("property", qual) / ("attr", type) / ("member", class) / ("classattr", class)
        # for a name on a class, looking through its bases.
        chain = [class_qual, *[base for base in self.base_chain(class_qual) if base in self.symbols]]
        for qual in chain:
            symbol = self.symbols[qual]
            for field in symbol.fields:
                if field["name"] == name:
                    return ("field", field.get("t"))
            method = f"{qual}.{name}"
            if method in self.symbols:
                target = self.symbols[method]
                return ("property", method) if target.subkind in ("property", "cached property") else ("method", method)
            for member in symbol.members:
                if member["name"] == name:
                    return ("member", qual)
            if name in symbol.inst_attrs:
                return ("attr", symbol.inst_attrs[name])
            for attr in symbol.class_attrs:
                if attr["name"] == name:
                    return ("classattr", qual)
        return None

    def implementations(self, method_qual: str) -> list[str]:
        # The methods that override this one in subclasses: what really runs when code calls it on the base class.
        method = self.symbols.get(method_qual)
        if method is None or method.owner is None:
            return []
        if self._subclasses is None:
            self._subclasses = {}
            for symbol in self.symbols.values():
                if symbol.kind in CLASS_KINDS:
                    for base in self.base_chain(symbol.qual):
                        self._subclasses.setdefault(base, []).append(symbol.qual)
        return [f"{sub}.{method.name}" for sub in self._subclasses.get(method.owner, [])
                if f"{sub}.{method.name}" in self.symbols]

    # -- scanning ------------------------------------------------------------------------------------------------------------

    def _scan(self) -> None:
        for symbol in self.symbols.values():
            if symbol.kind in ("function", "method"):
                symbol.events, symbol.refs = [], {}
                FunctionScan(self, symbol).run()
        if self.track:
            self._propagate_writes()

    def _propagate_writes(self) -> None:
        # A call to a method of the tracked class that itself writes fields (add_item calling self.replace(items=...)) writes
        # those fields too.
        own = {qual: [e["field"] for e in symbol.events if e["kind"] == "write"]
               for qual, symbol in self.symbols.items() if symbol.owner == self.track}
        for symbol in self.symbols.values():
            added = []
            for event in symbol.events:
                if event["kind"] == "call" and own.get(event.get("target")) and event.get("target") != symbol.qual:
                    for field in own[event["target"]]:
                        added.append((event, {"kind": "write", "line": event["line"], "field": field}))
            for event, write in added:
                symbol.events.insert(symbol.events.index(event) + 1, write)

    def _choose_track(self) -> str | None:
        # The object the code passes along and hands back: the class most functions take as a parameter and return, such as
        # a context, a state or a report. None when no class is passed along by at least two functions.
        scores: dict[str, int] = {}
        for symbol in self.symbols.values():
            if symbol.kind not in ("function", "method"):
                continue
            returns = strip_none(symbol.returns)
            if not (isinstance(returns, str) and returns in self.symbols) or symbol.owner == returns:
                continue
            if any(strip_none(param["t"]) == returns for param in symbol.params):
                scores[returns] = scores.get(returns, 0) + 1
        candidates = [qual for qual, score in scores.items() if score >= 2 and self.symbols[qual].fields]
        return max(candidates, key=lambda qual: scores[qual]) if candidates else None

    def scan_statements(self, module: str, statements: list) -> list[dict]:
        # The events of loose module-level code, such as an `if __name__ == "__main__":` block.
        fake = Symbol(f"{module}.<main>", "function", "<main>", module, SimpleNamespace(body=statements, lineno=1),
                      self.files[module])
        FunctionScan(self, fake).run()
        return fake.events


class Typer:
    # Works out the type of an expression, given the types of the names around it.

    def __init__(self, model: CodeModel, module: str, owner: str | None) -> None:
        self.model = model
        self.module = module
        self.owner = owner

    def symbol_ref(self, qual: str):
        kind = self.model.symbols[qual].kind
        if kind in CLASS_KINDS:
            return ("classref", qual)
        if kind == "function":
            return ("funcref", qual)
        if kind == "constant":
            return ("const", qual)
        return ("bound", qual)

    def name(self, name: str, env: dict):
        if name in env:
            return env[name]
        resolved = self.model.resolve(self.module, name)
        if isinstance(resolved, str) and resolved in self.model.symbols:
            return self.symbol_ref(resolved)
        if isinstance(resolved, tuple):
            return resolved
        if name in ("str", "int", "float", "bool"):
            return ("classref", name)
        return UNKNOWN

    def returns(self, qual: str, call: ast.Call | None = None, env: dict | None = None):
        # The return type of a function or method. Self means the class it was called on; a function whose return type is
        # a type variable it also takes as type[T] returns an instance of the class passed in.
        symbol = self.model.symbols.get(qual)
        if symbol is None:
            return UNKNOWN
        t = symbol.returns
        if t is None and symbol.owner and symbol.subkind == "classmethod":
            return symbol.owner
        if isinstance(t, str) and t not in self.model.symbols and call is not None:
            params = [p for p in symbol.params if not (symbol.owner and p["name"] in ("self", "cls"))]
            for index, param in enumerate(params):
                if param["t"] == ("type", t) and index < len(call.args):
                    passed = self.type_of(call.args[index], env or {})
                    return passed[1] if isinstance(passed, tuple) and passed[0] == "classref" else UNKNOWN
        return t

    def type_of(self, node, env: dict):
        model = self.model
        if isinstance(node, ast.Name):
            t = self.name(node.id, env)
            return model.const_type(t[1]) if isinstance(t, tuple) and t[0] == "const" and node.id not in env else t
        if isinstance(node, ast.Constant):
            return type(node.value).__name__ if node.value is not None else "None"
        if isinstance(node, ast.JoinedStr):
            return "str"
        if isinstance(node, ast.Attribute):
            base = strip_none(self.type_of(node.value, env))
            if isinstance(base, tuple) and base[0] == "module":
                found = model.member_of_module(base[1], node.attr)
                if isinstance(found, str):
                    ref = self.symbol_ref(found)
                    return model.const_type(found) if ref[0] == "const" else ref
                return found
            if isinstance(base, str) and base in model.symbols:
                member = model.class_member(base, node.attr)
                if member is None:
                    return UNKNOWN
                kind, value = member
                if kind in ("field", "attr"):
                    return value
                if kind == "property":
                    return self.returns(value)
                if kind == "method":
                    return ("bound", value)
                return UNKNOWN
            if isinstance(base, tuple) and base[0] == "classref" and base[1] in model.symbols:
                member = model.class_member(base[1], node.attr)
                if member is None:
                    return UNKNOWN
                kind, value = member
                if kind in ("method", "property"):
                    return ("bound", value)
                if kind == "member":
                    return base[1]
                return UNKNOWN
            if isinstance(base, tuple) and base[0] in ("list", "dict", "set", "tuple"):
                return ("container_method", base, node.attr)
            return UNKNOWN
        if isinstance(node, ast.Call):
            return self.call_type(node, env)
        if isinstance(node, ast.Await):
            return self.type_of(node.value, env)
        if isinstance(node, ast.Subscript):
            base = strip_none(self.type_of(node.value, env))
            if isinstance(node.slice, ast.Slice):
                return base
            if isinstance(base, tuple):
                if base[0] == "list":
                    return base[1]
                if base[0] == "dict":
                    return base[2]
                if base[0] == "tuple" and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int):
                    items = base[1]
                    return items[node.slice.value] if -len(items) <= node.slice.value < len(items) else UNKNOWN
            return "str" if base == "str" else UNKNOWN
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            return ("list", self.type_of(node.elt, self.comp_env(node, env)))
        if isinstance(node, ast.SetComp):
            return ("set", self.type_of(node.elt, self.comp_env(node, env)))
        if isinstance(node, ast.DictComp):
            inner = self.comp_env(node, env)
            return ("dict", self.type_of(node.key, inner), self.type_of(node.value, inner))
        if isinstance(node, ast.IfExp):
            body, orelse = self.type_of(node.body, env), self.type_of(node.orelse, env)
            return orelse if body in (None, "None") else body
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                t = self.type_of(value, env)
                if t is not None:
                    return t
            return UNKNOWN
        if isinstance(node, ast.Tuple):
            return ("tuple", [self.type_of(item, env) for item in node.elts])
        if isinstance(node, ast.List):
            return ("list", self.type_of(node.elts[0], env) if node.elts else UNKNOWN)
        if isinstance(node, ast.NamedExpr):
            return self.type_of(node.value, env)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.type_of(node.left, env)
            return left if isinstance(left, tuple) and left[0] == "list" else self.type_of(node.right, env)
        if isinstance(node, ast.Starred):
            return self.type_of(node.value, env)
        return UNKNOWN

    def comp_env(self, node, env: dict) -> dict:
        inner = dict(env)
        for generator in node.generators:
            self.bind(generator.target, elem_of(self.type_of(generator.iter, inner)), inner)
        return inner

    def call_type(self, node: ast.Call, env: dict):
        func = self.type_of(node.func, env)
        if isinstance(func, tuple):
            if func[0] == "bound":
                return self.returns(func[1], node, env)
            if func[0] == "classref":
                return func[1]
            if func[0] == "funcref":
                return self.returns(func[1], node, env)
            if func[0] == "container_method":
                _, base, method = func
                if base[0] == "dict":
                    if method in ("get", "setdefault", "pop"):
                        return base[2]
                    if method == "items":
                        return ("list", ("tuple", [base[1], base[2]]))
                    if method == "values":
                        return ("list", base[2])
                    if method == "keys":
                        return ("list", base[1])
                if base[0] == "list" and method in ("pop", "copy"):
                    return base[1] if method == "pop" else base
                return UNKNOWN
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("model_copy", "copy", "_replace", "evolve"):
            return self.type_of(node.func.value, env)
        called = ast.unparse(node.func)
        if called in ("replace", "dataclasses.replace", "copy.copy", "copy.deepcopy", "attr.evolve", "attrs.evolve",
                      "evolve", "cast", "typing.cast") and node.args:
            return self.type_of(node.args[-1] if called.endswith("cast") else node.args[0], env)
        if isinstance(node.func, ast.Name):
            name = node.func.id
            first = self.type_of(node.args[0], env) if node.args else UNKNOWN
            if name in ("sorted", "list", "reversed", "tuple", "filter"):
                return ("list", elem_of(first if name != "filter" else self.type_of(node.args[-1], env)))
            if name == "set":
                return ("set", elem_of(first))
            if name in ("next", "max", "min") and node.args:
                return first if name in ("max", "min") and len(node.args) > 1 else elem_of(first)
            if name == "zip":
                return ("list", ("tuple", [elem_of(self.type_of(arg, env)) for arg in node.args]))
            if name == "enumerate":
                return ("list", ("tuple", ["int", elem_of(first)]))
            if name == "range":
                return ("list", "int")
            if name in ("str", "repr", "format"):
                return "str"
            if name in ("int", "len", "sum"):
                return "int"
            if name in ("float", "round"):
                return "float"
            if name in ("bool", "any", "all", "isinstance", "callable"):
                return "bool"
            if name == "super" and self.owner:
                bases = [base for base in self.model.base_chain(self.owner) if base in self.model.symbols]
                return bases[0] if bases else UNKNOWN
        return UNKNOWN

    def bind(self, target, t, env: dict) -> None:
        # Gives a name (or each name in a tuple) its type.
        if isinstance(target, ast.Name):
            env[target.id] = t
        elif isinstance(target, (ast.Tuple, ast.List)):
            items = t[1] if isinstance(t, tuple) and t[0] == "tuple" and len(t[1]) == len(target.elts) else [UNKNOWN] * len(target.elts)
            for sub, sub_t in zip(target.elts, items):
                self.bind(sub, sub_t, env)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, ("list", UNKNOWN), env)


class FunctionScan:
    # Walks one function's body in order, keeping the type of every name it can, and records what the function does.

    def __init__(self, model: CodeModel, symbol: Symbol) -> None:
        self.model = model
        self.symbol = symbol
        self.typer = Typer(model, symbol.module, symbol.owner)
        self.source = symbol.source
        self.track = model.track

    def run(self) -> None:
        env = {param["name"].lstrip("*"): param["t"] for param in self.symbol.params}
        if self.symbol.owner:
            first = self.symbol.params[0]["name"] if self.symbol.params else None
            if self.symbol.subkind == "classmethod" and first:
                env[first] = ("classref", self.symbol.owner)
            elif self.symbol.subkind != "static" and first:
                env[first] = self.symbol.owner
        for statement in self.symbol.node.body:
            self.statement(statement, env)

    def event(self, kind: str, node, **data) -> None:
        self.symbol.events.append({"kind": kind, "line": getattr(node, "lineno", 0), **data})

    def ref(self, node, qual: str, attr: str | None = None) -> None:
        # Remembers where a name of the repo appears, so the source view can link it.
        if attr is not None:
            line, col = node.end_lineno, self.source.char_col(node.end_lineno, node.end_col_offset) - len(attr)
        else:
            line, col = node.lineno, self.source.char_col(node.lineno, node.col_offset)
        self.symbol.refs[(line, col)] = qual

    def tracked(self, t) -> bool:
        return self.track is not None and strip_none(t) == self.track

    def is_track_field(self, name: str) -> bool:
        return any(field["name"] == name for field in self.model.symbols[self.track].fields)

    # -- statements ----------------------------------------------------------------------------------------------------------

    def statement(self, node, env: dict) -> None:
        typer = self.typer
        if isinstance(node, ast.Assign):
            self.expr(node.value, env)
            t = typer.type_of(node.value, env)
            for target in node.targets:
                self.target(target, env)
                typer.bind(target, t, env)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                self.expr(node.value, env)
            self.target(node.target, env)
            if isinstance(node.target, ast.Name):
                env[node.target.id] = self.model.annotation(node.annotation, self.symbol.module, self.symbol.owner)
        elif isinstance(node, ast.AugAssign):
            self.expr(node.value, env)
            self.target(node.target, env)
            self.expr(node.target, env)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.expr(node.iter, env)
            typer.bind(node.target, elem_of(typer.type_of(node.iter, env)), env)
            for child in [*node.body, *node.orelse]:
                self.statement(child, env)
        elif isinstance(node, (ast.While, ast.If)):
            self.expr(node.test, env)
            for child in [*node.body, *node.orelse]:
                self.statement(child, env)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self.expr(item.context_expr, env)
                if item.optional_vars is not None:
                    typer.bind(item.optional_vars, typer.type_of(item.context_expr, env), env)
            for child in node.body:
                self.statement(child, env)
        elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            for child in node.body:
                self.statement(child, env)
            for handler in node.handlers:
                if handler.type is not None:
                    self.expr(handler.type, env)
                if handler.name:
                    env[handler.name] = UNKNOWN
                for child in handler.body:
                    self.statement(child, env)
            for child in [*node.orelse, *node.finalbody]:
                self.statement(child, env)
        elif isinstance(node, ast.Raise):
            if node.exc is not None:
                self.expr(node.exc, env)
                called = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                target = self.model.expr_target(self.symbol.module, called)
                qual = target if isinstance(target, str) and target in self.model.symbols else None
                self.event("raise", node, name=ast.unparse(called).rsplit(".", 1)[-1], target=qual)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = dict(env)
            for arg in node.args.args:
                inner[arg.arg] = self.model.annotation(arg.annotation, self.symbol.module, self.symbol.owner)
            for child in node.body:
                self.statement(child, inner)
        elif isinstance(node, ast.ClassDef):
            return
        elif isinstance(node, getattr(ast, "Match", ())):
            self.expr(node.subject, env)
            for case in node.cases:
                if case.guard is not None:
                    self.expr(case.guard, env)
                for child in case.body:
                    self.statement(child, env)
        elif isinstance(node, (ast.Return, ast.Expr)):
            if node.value is not None:
                self.expr(node.value, env)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self.expr(child, env)
                elif isinstance(child, ast.stmt):
                    self.statement(child, env)

    def target(self, node, env: dict) -> None:
        # Assignment targets can read things too (x[i] = ...), and can write a field of the tracked class (state.total = ...).
        if isinstance(node, ast.Subscript):
            self.expr(node.value, env)
            self.expr(node.slice, env)
        elif isinstance(node, ast.Attribute):
            self.expr(node.value, env)
            if self.track and self.tracked(self.typer.type_of(node.value, env)) and self.is_track_field(node.attr):
                self.event("write", node, field=node.attr)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                self.target(item, env)

    # -- expressions ---------------------------------------------------------------------------------------------------------

    def expr(self, node, env: dict) -> None:
        typer, model = self.typer, self.model
        if node is None:
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = dict(env)
            for generator in node.generators:
                self.expr(generator.iter, inner)
                typer.bind(generator.target, elem_of(typer.type_of(generator.iter, inner)), inner)
                for condition in generator.ifs:
                    self.expr(condition, inner)
            if isinstance(node, ast.DictComp):
                self.expr(node.key, inner)
                self.expr(node.value, inner)
            else:
                self.expr(node.elt, inner)
            return
        if isinstance(node, ast.Lambda):
            inner = dict(env)
            for arg in node.args.args:
                inner.setdefault(arg.arg, UNKNOWN)
            self.expr(node.body, inner)
            return
        if isinstance(node, ast.NamedExpr):
            self.expr(node.value, env)
            typer.bind(node.target, typer.type_of(node.value, env), env)
            return
        if isinstance(node, ast.Call):
            self.call(node, env)
            return
        if isinstance(node, ast.Attribute):
            self.attribute(node, env)
            self.expr(node.value, env)
            return
        if isinstance(node, ast.Name):
            t = typer.name(node.id, env) if node.id not in env else None
            if isinstance(t, tuple) and t[0] in ("classref", "funcref", "const") and t[1] in model.symbols:
                self.ref(node, t[1])
                if t[0] == "const":
                    self.event("const", node, target=t[1])
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.expr(child, env)
            elif isinstance(child, ast.keyword):
                self.expr(child.value, env)
            elif isinstance(child, ast.comprehension):
                self.expr(child.iter, env)

    def call(self, node: ast.Call, env: dict) -> None:
        typer, model = self.typer, self.model
        func = node.func
        self.call_arguments(node, env)
        callee = typer.type_of(func, env)
        target = None
        if isinstance(callee, tuple) and callee[0] in ("bound", "funcref", "classref") and callee[1] in model.symbols:
            target = callee[1]
        if isinstance(func, ast.Attribute) and func.attr == "__init__" and isinstance(func.value, ast.Call):
            base = typer.type_of(func.value, env)
            if isinstance(base, str) and f"{base}.__init__" in model.symbols:
                target = f"{base}.__init__"
        if target is not None:
            symbol = model.symbols[target]
            if symbol.kind in CLASS_KINDS:
                self.event("build", node, target=target)
                if target == self.track:
                    for kw in node.keywords:
                        if kw.arg and self.is_track_field(kw.arg):
                            self.event("write", node, field=kw.arg)
            else:
                self.event("call", node, target=target)
                if self.track and symbol.owner == self.track:
                    for kw in node.keywords:
                        if kw.arg and self.is_track_field(kw.arg):
                            self.event("write", node, field=kw.arg)
            if isinstance(func, ast.Attribute):
                self.ref(func, target, attr=func.attr)
            elif isinstance(func, ast.Name):
                self.ref(func, target)
            return
        if self.track and self.writes_through_copy(node, env):
            return
        if isinstance(func, ast.Subscript) and isinstance(func.value, (ast.Name, ast.Attribute)):
            registry = model.expr_target(self.symbol.module, func.value)
            built = []
            if isinstance(registry, str) and model.symbols.get(registry) and model.symbols[registry].kind == "constant":
                const = model.symbols[registry]
                value = const.node.value
                if isinstance(value, ast.Dict):
                    for item in value.values:
                        found = model.expr_target(const.module, item)
                        if isinstance(found, str) and found in model.symbols:
                            built.append(found)
            for qual in built:
                self.event("build" if model.symbols[qual].kind in CLASS_KINDS else "call", node, target=qual,
                           via=model.symbols[registry].name)
            if built:
                return
        self.event("external", node, name=ast.unparse(func)[:60])

    def writes_through_copy(self, node: ast.Call, env: dict) -> bool:
        # state.model_copy(update={"x": ...}), replace(state, x=...), evolve(state, x=...), state._replace(x=...): each writes
        # fields of the tracked class. Returns whether the call was one of these.
        func = node.func
        fields: list[str] = []
        if isinstance(func, ast.Attribute) and func.attr in ("model_copy", "_replace", "copy"):
            if not self.tracked(self.typer.type_of(func.value, env)):
                return False
            fields = [kw.arg for kw in node.keywords if kw.arg]
            for kw in node.keywords:
                if kw.arg == "update" and isinstance(kw.value, ast.Dict):
                    fields += [key.value for key in kw.value.keys if isinstance(key, ast.Constant)]
        elif ast.unparse(func) in ("replace", "dataclasses.replace", "evolve", "attr.evolve", "attrs.evolve") and node.args:
            if not self.tracked(self.typer.type_of(node.args[0], env)):
                return False
            fields = [kw.arg for kw in node.keywords if kw.arg]
        else:
            return False
        for field in fields:
            if self.is_track_field(field):
                self.event("write", node, field=field)
        self.event("external", node, name=ast.unparse(func)[:60])
        return True

    def call_arguments(self, node: ast.Call, env: dict) -> None:
        # Everything a call evaluates before it runs: the object it's called on, then its arguments.
        typer = self.typer
        func = node.func
        lambda_item = UNKNOWN   # sorted(x, key=lambda item: ...): the lambda's argument is an item of x
        if isinstance(func, ast.Name) and func.id in ("sorted", "max", "min") and node.args:
            lambda_item = elem_of(typer.type_of(node.args[0], env))
        elif isinstance(func, ast.Attribute) and func.attr == "sort":
            lambda_item = elem_of(typer.type_of(func.value, env))
        if isinstance(func, ast.Attribute):
            self.expr(func.value, env)
        elif not isinstance(func, ast.Name):
            self.expr(func, env)
        for arg in node.args:
            self.expr(arg, env)
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Lambda) and kw.value.args.args:
                inner = dict(env)
                inner[kw.value.args.args[0].arg] = lambda_item
                self.expr(kw.value.body, inner)
            else:
                self.expr(kw.value, env)

    def attribute(self, node: ast.Attribute, env: dict) -> None:
        # A read of a field, of the tracked class's fields, of a property (which runs code, so it counts as a call), of a
        # module's function or constant, or of an enum member.
        typer, model = self.typer, self.model
        base = strip_none(typer.type_of(node.value, env))
        if isinstance(base, tuple) and base[0] == "module":
            found = model.member_of_module(base[1], node.attr)
            if isinstance(found, str) and found in model.symbols:
                self.ref(node, found, attr=node.attr)
                if model.symbols[found].kind == "constant":
                    self.event("const", node, target=found)
            return
        if isinstance(base, str) and base in model.symbols:
            member = model.class_member(base, node.attr)
            if member is None:
                return
            kind, value = member
            if kind == "field" and base == self.track:
                self.event("read", node, field=node.attr)
            elif kind == "field":
                self.event("field", node, owner=base, field=node.attr)
            elif kind == "property":
                self.event("call", node, target=value)
                self.ref(node, value, attr=node.attr)
            elif kind == "attr" and isinstance(node.value, ast.Name) and node.value.id == "self":
                self.event("attr", node, field=node.attr)
        elif isinstance(base, tuple) and base[0] == "classref" and base[1] in model.symbols:
            member = model.class_member(base[1], node.attr)
            if member and member[0] == "member":
                self.event("enum", node, owner=base[1], field=node.attr)
