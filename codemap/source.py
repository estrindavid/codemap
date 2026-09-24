# Reading a repository: which Python files belong to it, what each one is called when imported, and each file's comments
# and syntax-highlighted lines.
import ast
import html
import io
import keyword
import builtins
import subprocess
import tokenize
from pathlib import Path, PurePosixPath

BUILTIN_NAMES = set(dir(builtins))
SKIP_DIRS = {".git", ".hg", ".venv", "venv", "env", ".env", "node_modules", "__pycache__", "build", "dist", "site-packages",
             ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs", ".idea", ".vscode"}


# --- git -------------------------------------------------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    # Runs one git command in the repo and returns its output.
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def is_git(repo: Path) -> bool:
    try:
        return git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


def git_prefix(repo: Path) -> str:
    # Where the repo folder sits inside its git checkout ("" at the top, "tools/app/" in a monorepo).
    return git(repo, "rev-parse", "--show-prefix").strip()


def main_checkout(path: Path) -> Path:
    # The main checkout of a repo, even when path is one of its worktrees; path itself when it isn't in git.
    if not is_git(path):
        return path
    common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    top = Path(git(path, "rev-parse", "--show-toplevel").strip())
    return common.parent if common.name == ".git" else top


# --- which files --------------------------------------------------------------------------------------------------------

def is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (any(part in ("test", "tests", "testing") for part in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


def list_files(repo: Path, ref: str | None, include_tests: bool = False, only: list[str] | None = None) -> list[str]:
    # The repo's .py files, as repo-relative posix paths: git's tracked and untracked-but-not-ignored files, or every file at
    # a ref; outside git (or where git ignores everything), every file not in a virtualenv, cache or build folder. Tests are
    # left out unless asked for.
    if ref is not None:
        prefix = git_prefix(repo)
        names = [name.removeprefix(prefix) for name in git(repo, "ls-tree", "-r", "--name-only", "--full-tree", ref).split("\n")
                 if name.startswith(prefix)]
    else:
        names = []
        if is_git(repo):
            names = [name for name in git(repo, "ls-files", "--cached", "--others", "--exclude-standard").split("\n")
                     if name.endswith(".py") and (repo / name).is_file()]
        if not names:   # not in git, or a folder git ignores (such as an installed package): walk it
            names = [path.relative_to(repo).as_posix() for path in repo.rglob("*.py")
                     if not any(part in SKIP_DIRS for part in path.relative_to(repo).parts)]
    names = [name for name in names if name.endswith(".py") and not any(part in SKIP_DIRS for part in PurePosixPath(name).parts)]
    if not include_tests:
        names = [name for name in names if not is_test(name)]
    if only:
        roots = [root.strip("/") + "/" for root in only]
        names = [name for name in names if any(name.startswith(root) or name == root.rstrip("/") for root in roots)]
    return sorted(set(names))


def read_sources(repo: Path, ref: str | None, names: list[str]) -> dict[str, str]:
    # Each file's text, from the working tree or from a git ref.
    if ref is None:
        return {name: (repo / name).read_text(encoding="utf-8", errors="replace") for name in names}
    prefix = git_prefix(repo)
    return {name: git(repo, "show", f"{ref}:{prefix}{name}") for name in names}


def read_file(repo: Path, ref: str | None, rel: str) -> str | None:
    # One other file (pyproject.toml, an order file) from the working tree or a ref; None if it isn't there.
    try:
        if ref is None:
            return (repo / rel).read_text(encoding="utf-8")
        return git(repo, "show", f"{ref}:{git_prefix(repo)}{rel}")
    except (OSError, subprocess.CalledProcessError):
        return None


# --- module names ---------------------------------------------------------------------------------------------------------

def module_names(paths: list[str], repo_name: str) -> dict[str, str]:
    # The name each file is imported by: src/pkg/sub/mod.py -> pkg.sub.mod, climbing only through folders that hold an
    # __init__.py. When the repo folder itself is a package (an installed library), its name comes first. Two files that would
    # get the same name keep their whole path instead.
    packages = {PurePosixPath(path).parent.as_posix() for path in paths if PurePosixPath(path).name == "__init__.py"}
    names = {}
    for path in paths:
        pure = PurePosixPath(path)
        parts = [pure.stem]
        folder = pure.parent
        while folder.as_posix() in packages:
            if folder.as_posix() == ".":
                parts.insert(0, repo_name)
                break
            parts.insert(0, folder.name)
            folder = folder.parent
        if parts[-1] == "__init__":
            parts.pop()
        names[path] = ".".join(parts) or repo_name
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    for path, name in names.items():
        if counts[name] > 1:
            names[path] = PurePosixPath(path).with_suffix("").as_posix().replace("/", ".")
    return names


# --- comments and highlighting ------------------------------------------------------------------------------------------

def first_line(node) -> int:
    # Where a statement starts, counting its decorators.
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])


class SourceFile:
    # One module's text, lines, comments by line and tokens, with the helpers that read comments around statements.

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self.lines = text.split("\n")
        self.comments: dict[int, str] = {}
        try:
            self.tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, SyntaxError):
            self.tokens = []
        for token in self.tokens:
            if token.type == tokenize.COMMENT:
                self.comments[token.start[0]] = token.string[1:].strip()

    def standalone(self, line: int) -> bool:
        # True if the line holds only a comment.
        return 1 <= line <= len(self.lines) and self.lines[line - 1].strip().startswith("#")

    def comment_above(self, line: int) -> list[str]:
        # The block of comment-only lines directly above a line, top to bottom; a blank line ends the block.
        block = []
        row = line - 1
        while self.standalone(row):
            block.append(self.comments.get(row, self.lines[row - 1].strip().lstrip("#").strip()))
            row -= 1
        return block[::-1]

    def doc_comment(self, node) -> list[str]:
        # A def's or class's own words: its docstring, else the comment right above it (above its decorators, or between them
        # and the def line), else the comment its body opens with.
        body = getattr(node, "body", [])
        first = body[0] if body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            return [line.strip() for line in first.value.value.strip().splitlines() if line.strip()]
        top = first_line(node)
        found = self.comment_above(top)
        if node.lineno != top:
            found += self.comment_above(node.lineno)
        if found or first is None:
            return found
        return self.comment_above(first_line(first))

    def trailing(self, node) -> list[str]:
        # The comment at the end of a statement's lines, plus comment-only lines right below it that are indented deeper.
        found = []
        for row in range(node.lineno, node.end_lineno + 1):
            if row in self.comments and not self.standalone(row):
                found.append(self.comments[row])
        indent = len(self.lines[node.lineno - 1]) - len(self.lines[node.lineno - 1].lstrip())
        row = node.end_lineno + 1
        while self.standalone(row):
            text = self.lines[row - 1]
            if len(text) - len(text.lstrip()) <= indent:
                break
            found.append(self.comments.get(row, ""))
            row += 1
        return found

    def header(self) -> list[str]:
        # The comment block or docstring a module opens with.
        block = []
        for row in range(1, len(self.lines) + 1):
            if self.standalone(row):
                if not self.lines[row - 1].startswith("#!"):
                    block.append(self.comments.get(row, ""))
            else:
                break
        return block

    def char_col(self, line: int, byte_col: int) -> int:
        # ast columns count bytes; tokenize and the browser count characters.
        return len(self.lines[line - 1].encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))

    def highlight(self, refs: dict[tuple[int, int], str]) -> list[str]:
        # The file as HTML, one string per line: keywords, strings, comments and numbers marked, and every name that points at
        # a symbol of the repo made clickable.
        out = [""] * len(self.lines)
        filled = [0] * len(self.lines)
        previous = None
        for token in self.tokens:
            if token.type in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER,
                              tokenize.ENCODING):
                continue
            css = self._css(token, previous)
            ref = refs.get(token.start) if token.type == tokenize.NAME else None
            (start_row, start_col), (end_row, end_col) = token.start, token.end
            for row in range(start_row, end_row + 1):
                if row > len(self.lines):
                    break
                line = self.lines[row - 1]
                begin = max(start_col if row == start_row else 0, filled[row - 1])
                end = end_col if row == end_row else len(line)
                if begin > filled[row - 1]:
                    out[row - 1] += html.escape(line[filled[row - 1]:begin])
                piece = html.escape(line[begin:end])
                if piece:
                    if ref:
                        out[row - 1] += f'<a class="ref {css}" data-ref="{html.escape(ref)}">{piece}</a>'
                    else:
                        out[row - 1] += f'<span class="{css}">{piece}</span>' if css else piece
                filled[row - 1] = max(filled[row - 1], end)
            if token.type != tokenize.COMMENT:
                previous = token
        for row, line in enumerate(self.lines):
            if filled[row] < len(line):
                out[row] += html.escape(line[filled[row]:])
        return out

    @staticmethod
    def _css(token: tokenize.TokenInfo, previous: tokenize.TokenInfo | None) -> str:
        # The highlight class for one token.
        kind = token.type
        if kind == tokenize.COMMENT:
            return "t-com"
        if kind == tokenize.STRING or tokenize.tok_name.get(kind, "").startswith(("FSTRING", "TSTRING")):
            return "t-str"
        if kind == tokenize.NUMBER:
            return "t-num"
        if kind == tokenize.OP:
            return "t-op"
        if kind == tokenize.NAME:
            word = token.string
            if previous is not None and previous.type == tokenize.NAME and previous.string in ("def", "class"):
                return "t-def"
            if previous is not None and previous.type == tokenize.OP and previous.string == "@":
                return "t-dec"
            if keyword.iskeyword(word):
                return "t-kw"
            if word in ("self", "cls"):
                return "t-self"
            if word in BUILTIN_NAMES:
                return "t-bi"
            return "t-name"
        return ""
