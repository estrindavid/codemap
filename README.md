# codemap

An interactive, local map of a Python codebase, laid out in the order to read it.

Point it at a repo and it opens a page where every function, class and constant is a box, arranged in columns that follow
one run of the program: it starts where the code starts (your `pyproject.toml` command, an `if __name__ == "__main__":`
block, a `main()`), and walks every call in the order it happens, so each helper sits right after the code that first
uses it and each class right before the code that first builds or reads it. Click a box to see its code, what it calls,
who calls it and which fields it reads, with those connections drawn on the map. Mark boxes as read and take notes as you
go: they're saved outside the repo and follow code that moves.

It reads your code, never runs it, and needs nothing but Python 3.11+.

## Run it

```bash
pip install git+https://github.com/estrindavid/codemap
codemap path/to/your/repo --open
```

or, from a clone of this repo:

```bash
python -m codemap path/to/your/repo
```

then open http://127.0.0.1:8777. The map rebuilds itself whenever the code changes, and the dropdown shows any git branch
instead of the working tree. Use a Python at least as new as the syntax your code uses.

## What you see

- **Columns are chapters** of the reading order, about a dozen boxes each (`--chapter-size`). The sidebar lists them, with
  your progress. `j`/`k` step through the whole order, `r` marks the current box read.
- **Boxes** show a function's signature and description (its docstring, or the comment above it), then what it reads,
  writes, calls and builds. Classes show their fields, records (dataclasses, pydantic models, NamedTuples) their typed
  fields, enums their values.
- **Lines across the top** are the fields of the object the code passes along, if it has one: the class most functions
  take as an argument and return, such as a context, a state or a report. Each line starts where a field is written and
  runs past every function that reads it. Pick the class yourself with `--track ClassName`, or turn it off with
  `--track none`.
- **Calls through a base class** follow on to the subclasses: when code calls `step.run()` on an abstract `Step`, the walk
  continues into each subclass's `run`, in the order the code builds them.

## Your notes

Read marks and notes live in `~/.codemap/<repo>/notes.json`, never in your repo, and every worktree of a repo shares them.
Each is saved under the id of what it's about (`records.Graph.node`). When code moves to another module
(`models.Node` becomes `records.Node`), the map moves its notes along on the next build, after backing the file up.

## A hand-written order

The walk is a good first draft. To set the first chapters yourself, add `.codemap/order.py` to your repo:

```python
CHAPTERS = [
    {
        "title": "The data, first",
        "goal": "Everything the program passes around.",   # optional, shown at the top of the column
        "items": [
            ("records.Node", "One step of the workflow."),     # (id, a note shown on the box)
            "records.Graph",                                   # or just the id
            ("cli.main", "Only the top half for now.", {"part": "start", "from": "parser =", "to": "raise SystemExit"}),
        ],
    },
]
```

Ids are what the map shows (click a box to see one). An id that stops matching the code is flagged on the map, and
everything the chapters leave out is placed by the walk after them.

## Options

| | |
|---|---|
| `--port 8777` | where to serve the page |
| `--only src/app` | map only these folders (repeatable) |
| `--tests` | include test files (left out by default) |
| `--entry cli.main` | start the walk here (repeatable), before anything found on its own |
| `--track Report` / `--track none` | the class whose fields become lines across the top |
| `--order FILE` | a hand-written order somewhere other than `.codemap/order.py` |
| `--chapter-size 12` | about how many boxes per column |
| `--notes FILE` | keep notes somewhere else |
| `--open` | open the page in your browser |

## How it works

- `codemap/source.py` finds the files (what git knows about, or every `.py` outside virtualenvs and build folders), works
  out each one's import name, and reads comments and syntax.
- `codemap/model.py` parses every file with `ast` and follows the code: imports (relative ones and re-exports too), classes
  and their bases, and, for every function, what it does in order. Types are followed just far enough to know which
  method a call lands on: annotations, constructors, return types and `self` attributes.
- `codemap/order.py` finds the entry points and walks the calls into chapters.
- `codemap/build.py` puts it together as the JSON the page reads, `codemap/serve.py` serves it, and `codemap/notes.py`
  keeps your notes.
- `codemap/static/` is the page: plain HTML, CSS and JavaScript, no build step.

It's static analysis, so it can't see calls made by name (`getattr`, plugin hooks, callbacks registered at runtime) or
through untyped values. Those boxes still get a place in the order, in a chapter per module after the walk.

## Develop

```bash
pip install -e ".[dev]"
pytest
```

`tests/sample` is a small repo with a bit of everything the map understands.
