import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from codemap import notes
from codemap.build import Options, build_map
from codemap.serve import Server, handler_for
from codemap.source import list_files, module_names

SAMPLE = Path(__file__).parent / "sample"


@pytest.fixture(scope="module")
def shop() -> dict:
    return build_map(SAMPLE)


def events(data: dict, kind: str) -> list[str]:
    return [e.get("target") or e.get("field") for e in data.get("events", []) if e["kind"] == kind]


def position(shop: dict) -> dict[str, int]:
    order = [item["ref"] for chapter in shop["chapters"] for item in chapter["items"]]
    return {ref: index for index, ref in enumerate(order)}


# --- reading the repo --------------------------------------------------------------------------------------------------------

def test_files_leave_out_tests_and_get_import_names():
    paths = list_files(SAMPLE, None)
    assert "tests/test_shop.py" not in paths
    assert "src/shop/cli.py" in paths and "scripts/report.py" in paths
    names = module_names(paths, "sample")
    assert names["src/shop/cli.py"] == "shop.cli"
    assert names["src/shop/__init__.py"] == "shop"
    assert names["scripts/report.py"] == "report"


def test_a_folder_that_is_a_package_names_its_modules_after_itself():
    names = module_names(["__init__.py", "core.py", "sub/__init__.py", "sub/deep.py"], "lib")
    assert names == {"__init__.py": "lib", "core.py": "lib.core", "sub/__init__.py": "lib.sub", "sub/deep.py": "lib.sub.deep"}


def test_kinds(shop):
    kinds = {sid: shop["symbols"][sid]["kind"] for sid in
             ("shop.models.Item", "shop.models.Kind", "shop.models.ShopError", "shop.cart.Cart", "shop.steps.Step",
              "shop.cart.STEPS", "shop.cli.main", "shop.cart.Cart.count")}
    assert kinds == {"shop.models.Item": "record", "shop.models.Kind": "enum", "shop.models.ShopError": "error",
                     "shop.cart.Cart": "record", "shop.steps.Step": "class", "shop.cart.STEPS": "constant",
                     "shop.cli.main": "function", "shop.cart.Cart.count": "method"}
    assert shop["symbols"]["shop.cart.Cart.count"]["subkind"] == "property"


def test_comments_above_a_def_are_its_description(shop):
    assert shop["symbols"]["shop.loader.load"]["comment"] == ["Reads one item per line."]
    assert shop["symbols"]["shop.models.Item"]["comment"] == ["One thing in the shop."]
    assert shop["symbols"]["shop.cart.checkout"]["code"][0]["text"].startswith("# Prices the cart")


# --- following the code ------------------------------------------------------------------------------------------------------

def test_calls_across_modules_and_relative_imports(shop):
    main = shop["symbols"]["shop.cli.main"]
    assert events(main, "call") == ["shop.loader.load", "shop.cart.checkout"]
    assert events(main, "build") == ["shop.cart.Cart"]
    assert [(e["owner"], e["field"]) for e in main["events"] if e["kind"] == "enum"] == [("shop.models.Kind", "food")]


def test_a_reexport_resolves_to_the_real_class(shop):
    assert events(shop["symbols"]["shop.loader.load"], "build") == ["shop.models.Item"]


def test_calls_outside_the_repo_leave_out_methods_on_local_values(shop):
    load = shop["symbols"]["shop.loader.load"]
    assert [e["name"] for e in load["events"] if e["kind"] == "external"] == ["open"]   # not line.strip


def test_a_property_read_counts_as_a_call(shop):
    assert "shop.cart.checkout" in shop["symbols"]["shop.cart.Cart.count"]["called_by"]


def test_a_typed_constant_types_its_items(shop):
    checkout = shop["symbols"]["shop.cart.checkout"]
    assert "shop.steps.Step.apply" in events(checkout, "call")
    assert shop["symbols"]["shop.cart.STEPS"]["uses"] == ["shop.steps.Discount", "shop.steps.Tax"]
    assert "shop.cart.STEPS" in shop["symbols"]["shop.steps.Tax"]["called_by"]
    assert shop["implemented_by"]["shop.steps.Step.apply"] == ["shop.steps.Discount.apply", "shop.steps.Tax.apply"]


def test_raises(shop):
    raised = [e for e in shop["symbols"]["shop.cart.checkout"]["events"] if e["kind"] == "raise"]
    assert raised == [{"kind": "raise", "line": raised[0]["line"], "name": "ShopError", "target": "shop.models.ShopError"}]


# --- the tracked object ------------------------------------------------------------------------------------------------------

def test_the_object_passed_along_is_tracked(shop):
    assert shop["track"]["id"] == "shop.cart.Cart"
    fields = {field["name"]: field for field in shop["track"]["fields"]}
    assert fields["items"]["writers"] == ["shop.cli.main"]
    assert set(fields["total"]["writers"]) == {"shop.cart.add_prices", "shop.steps.Discount.apply", "shop.steps.Tax.apply"}
    assert "shop.cart.add_prices" in fields["items"]["readers"]
    assert "shop.cli.main" in fields["total"]["readers"]


def test_tracking_can_be_turned_off():
    assert build_map(SAMPLE, options=Options(track=None))["track"] is None


# --- the reading order -------------------------------------------------------------------------------------------------------

def test_entries(shop):
    assert shop["entries"][0] == "shop.cli.main"
    assert "the `shop` command" in shop["entry_notes"][0]


def test_every_symbol_is_placed_once_in_the_order_it_runs(shop):
    order = [item["ref"] for chapter in shop["chapters"] for item in chapter["items"]]
    assert sorted(order) == sorted(shop["symbols"])
    at = position(shop)
    assert order[0] == "shop.cli.main"
    assert at["shop.cli.main"] < at["shop.loader.load"] < at["shop.models.Item"] < at["shop.cart.checkout"]
    assert at["shop.cart.checkout"] < at["shop.cart.add_prices"] < at["shop.steps.Step.apply"]
    assert at["shop.steps.Step.apply"] < at["shop.steps.Discount.apply"] < at["shop.steps.Tax.apply"]
    assert at["shop.steps.Discount"] < at["shop.steps.Tax"]


def test_chapters_stay_small():
    small = build_map(SAMPLE, options=Options(chapter_size=4))
    assert max(len(chapter["items"]) for chapter in small["chapters"]) <= 6
    assert len(small["chapters"]) > 2


def test_an_order_file_sets_the_first_chapters(tmp_path):
    repo = copy_sample(tmp_path)
    (repo / ".codemap").mkdir()
    (repo / ".codemap" / "order.py").write_text(
        'CHAPTERS = [{"title": "The data first", "items": [("shop.models.Kind", "read me first"), "shop.gone.Thing"]}]\n')
    data = build_map(repo)
    first = data["chapters"][0]
    assert first["title"] == "The data first" and first["section"] == "hand"
    assert first["items"][0] == {"ref": "shop.models.Kind", "note": "read me first"}
    assert first["items"][1]["stale"] is True
    assert any("shop.gone.Thing" in warning for warning in data["warnings"])
    refs = [item["ref"] for chapter in data["chapters"][1:] for item in chapter["items"]]
    assert "shop.models.Kind" not in refs and "shop.cli.main" in refs


# --- robustness --------------------------------------------------------------------------------------------------------------

def copy_sample(tmp_path: Path) -> Path:
    repo = tmp_path / "shop"
    shutil.copytree(SAMPLE, repo)
    return repo


def test_a_file_that_doesnt_parse_is_skipped(tmp_path):
    repo = copy_sample(tmp_path)
    (repo / "src" / "shop" / "broken.py").write_text("def oops(:\n")
    data = build_map(repo)
    assert data["skipped_files"] == 1
    assert "src/shop/broken.py" in data["warnings"][0]
    assert "shop.cli.main" in data["symbols"]


def test_a_git_ref(tmp_path):
    repo = copy_sample(tmp_path)
    git = lambda *args: subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init", "-q")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    cli = repo / "src" / "shop" / "cli.py"
    cli.write_text(cli.read_text().replace("def main()", "def run_shop()"))
    assert "shop.cli.run_shop" in build_map(repo)["symbols"]
    at_head = build_map(repo, "HEAD")
    assert "shop.cli.main" in at_head["symbols"] and "shop.cli.run_shop" not in at_head["symbols"]
    assert at_head["source"]["ref"] == "HEAD"


# --- notes -------------------------------------------------------------------------------------------------------------------

def test_notes_follow_code_that_moved(tmp_path, shop):
    path = tmp_path / "notes.json"
    notes.save(path, {"items": {"shop.old_cart.Cart.count": {"read": True}, "shop.cli.main#start": {"read": True},
                                "shop.nowhere.vanished": {"note": "gone"}, "input:graph": {"read": True}}})
    moves, gone = notes.follow_moves(path, shop["symbols"])
    assert moves == [("shop.old_cart.Cart.count", "shop.cart.Cart.count")]
    assert gone == ["shop.nowhere.vanished"]
    saved = notes.load(path)["items"]
    assert saved["shop.cart.Cart.count"]["read"] is True and "shop.old_cart.Cart.count" not in saved
    assert "shop.cli.main#start" in saved and "input:graph" in saved
    assert list(tmp_path.glob("notes.backup-*.json"))


def test_notes_update(tmp_path):
    path = tmp_path / "deep" / "notes.json"
    notes.update(path, "shop.cli.main", {"read": True})
    notes.update(path, "shop.cli.main", {"note": "starts here"})
    entry = notes.load(path)["items"]["shop.cli.main"]
    assert entry["read"] is True and entry["note"] == "starts here"


# --- the server --------------------------------------------------------------------------------------------------------------

def fetch(url: str, body: dict | None = None) -> bytes:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return response.read()


def test_the_server(tmp_path):
    server = Server(SAMPLE, Options(), tmp_path / "notes.json")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(server))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert b"codemap" in fetch(base + "/")
        assert b"function" in fetch(base + "/static/app.js")
        assert json.loads(fetch(base + "/api/map"))["entries"][0] == "shop.cli.main"
        assert json.loads(fetch(base + "/api/notes", {"id": "shop.cli.main", "read": True}))["ok"] is True
        assert json.loads(fetch(base + "/api/notes"))["items"]["shop.cli.main"]["read"] is True
        with pytest.raises(urllib.error.HTTPError) as caught:
            fetch(base + "/static/../serve.py")
        caught.value.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
