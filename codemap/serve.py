# Serves the map of a repo at http://127.0.0.1:8777 and keeps it current: the page rebuilds whenever the code changes, and
# can show any branch. Your notes and read marks are saved in ~/.codemap, outside the repo.
import argparse
import json
import subprocess
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codemap import notes
from codemap.build import Options, build_map, fingerprint
from codemap.source import git, is_git, main_checkout

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml"}


class Server:
    # One repo's map: builds it (reusing the last build while the code hasn't changed) and keeps its notes.

    def __init__(self, repo: Path, options: Options, notes_path: Path) -> None:
        self.repo = repo
        self.options = options
        self.notes_path = notes_path
        self.cache: dict[str | None, tuple[str, dict]] = {}
        self.lock = threading.Lock()

    def map(self, ref: str | None) -> dict:
        print_ = fingerprint(self.repo, ref, self.options)
        with self.lock:
            cached = self.cache.get(ref)
            if cached and cached[0] == print_:
                return cached[1]
            data = build_map(self.repo, ref, self.options)
            if ref is None and not data["skipped_files"] and not self.options.only:
                moves, gone = notes.follow_moves(self.notes_path, data["symbols"])
                data["warnings"] += [f"your notes on {old} moved with the code to {new}" for old, new in moves]
                if gone:
                    data["warnings"].append(f"{len(gone)} of your notes are on code that's gone: {', '.join(gone[:6])}"
                                            + (" …" if len(gone) > 6 else ""))
            self.cache[ref] = (print_, data)
            return data

    def refs(self) -> dict:
        # The branches the page can show, besides the working tree.
        if not is_git(self.repo):
            return {"current": "", "refs": []}
        names = git(self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").split()
        branch = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        return {"current": branch, "refs": [name for name in names if not name.endswith("/HEAD") and name != "origin"]}


def handler_for(server: Server):
    class Handler(BaseHTTPRequestHandler):
        # GET /, /static/*, /api/map, /api/refs, /api/fingerprint, /api/notes; POST /api/notes.

        def log_message(self, format, *args) -> None:
            pass

        def send(self, status: int, body: bytes, kind: str = "application/json; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def json(self, data, status: int = 200) -> None:
            self.send(status, json.dumps(data).encode())

        def do_GET(self) -> None:
            url = urlparse(self.path)
            ref = (parse_qs(url.query).get("ref") or [None])[0] or None
            try:
                if url.path in ("/", "/index.html"):
                    self.send(200, (STATIC / "index.html").read_bytes(), TYPES[".html"])
                elif url.path.startswith("/static/"):
                    path = (STATIC / url.path.removeprefix("/static/")).resolve()
                    if STATIC not in path.parents or not path.is_file():
                        self.send(404, b"not found", "text/plain")
                        return
                    self.send(200, path.read_bytes(), TYPES.get(path.suffix, "application/octet-stream"))
                elif url.path == "/api/map":
                    self.json(server.map(ref))
                elif url.path == "/api/refs":
                    self.json(server.refs())
                elif url.path == "/api/fingerprint":
                    self.json({"fingerprint": fingerprint(server.repo, ref, server.options)})
                elif url.path == "/api/notes":
                    self.json(notes.load(server.notes_path))
                else:
                    self.send(404, b"not found", "text/plain")
            except subprocess.CalledProcessError as exc:
                self.json({"error": f"git failed: {(exc.stderr or '').strip() or exc}"}, 400)
            except Exception as exc:   # show the problem in the page instead of a dead server
                self.json({"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()}, 500)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/notes":
                self.send(404, b"not found", "text/plain")
                return
            try:
                change = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                entry = notes.update(server.notes_path, str(change["id"]), change)
            except (json.JSONDecodeError, KeyError, TypeError):
                self.json({"error": "expected JSON with an id"}, 400)
                return
            self.json({"ok": True, "entry": entry})

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codemap", description="An interactive map of a Python codebase, in reading order.")
    parser.add_argument("repo", nargs="?", type=Path, default=Path("."), help="the repo to map (default: this folder)")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--only", action="append", default=[], metavar="FOLDER",
                        help="map only this folder (repeat for more); default: every .py file git knows about")
    parser.add_argument("--tests", action="store_true", help="include test files")
    parser.add_argument("--entry", action="append", default=[], metavar="NAME",
                        help="start the reading order here, e.g. cli.main (repeat for more)")
    parser.add_argument("--track", default="auto", metavar="CLASS",
                        help="draw this class's fields as lines across the map (default: the object the code passes along; "
                             "'none' to turn off)")
    parser.add_argument("--order", type=Path, default=None, help="a hand-written reading order (default: .codemap/order.py)")
    parser.add_argument("--chapter-size", type=int, default=12, help="about how many boxes per column (default: 12)")
    parser.add_argument("--notes", type=Path, default=None, help="where to keep your notes (default: ~/.codemap/<repo>/)")
    parser.add_argument("--open", action="store_true", help="open the map in your browser")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    if not repo.is_dir():
        parser.error(f"{repo} is not a folder")
    options = Options(only=args.only, include_tests=args.tests, entries=args.entry,
                      track=None if args.track.lower() == "none" else args.track, order=args.order,
                      chapter_size=max(3, args.chapter_size))
    notes_path = args.notes or notes.default_path(main_checkout(repo))
    server = Server(repo, options, notes_path)
    try:
        data = server.map(None)
    except Exception as exc:
        raise SystemExit(f"codemap: couldn't read {repo}: {exc}")
    if not data["symbols"]:
        raise SystemExit(f"codemap: no Python code found in {repo}")
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(server))
    url = f"http://127.0.0.1:{args.port}"
    print(f"codemap: {len(data['symbols'])} symbols in {len(data['modules'])} files, {len(data['chapters'])} chapters", flush=True)
    print(f"  starts at: {', '.join(data['entry_notes'][:4]) or '-'}", flush=True)
    print(f"  notes: {notes_path}", flush=True)
    print(f"  open {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
