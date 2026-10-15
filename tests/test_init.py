"""`overleaf-sync init`: set up the current folder from a project URL."""

import argparse
import os
import tomllib
from pathlib import Path

import pytest

import overleaf_sync.sync as overleaf_sync
from overleaf_sync.sync import parse_project_ref, render_config, ensure_gitignore, resolve_settings, load_config, run_init

PID = "6acf945e5bec579b3cef366d"


@pytest.mark.parametrize("ref,expected", [
    (PID, (PID, None)),
    (f"https://www.overleaf.com/project/{PID}", (PID, None)),
    (f"https://www.overleaf.com/project/{PID}?x=1#a", (PID, None)),
    (f"overleaf.com/project/{PID}", (PID, None)),
    (f"https://git.overleaf.com/{PID}", (PID, None)),
    (f"  https://www.overleaf.com/project/{PID}  ", (PID, None)),
    (f"http://localhost:8080/project/{PID}", (PID, "http://localhost:8080")),
    (f"https://latex.uni.edu/project/{PID}/", (PID, "https://latex.uni.edu")),
])
def test_project_refs(ref, expected):
    assert parse_project_ref(ref) == expected


@pytest.mark.parametrize("bad", ["", "paper", "https://www.overleaf.com/project/xyz", "https://www.overleaf.com/"])
def test_bad_refs_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_project_ref(bad)


def test_rendered_config_is_valid_toml_and_has_no_secret():
    text = render_config(PID, "http://localhost:8080", "overleaf.sid", "/home/u/cookie")
    cfg = tomllib.loads(text)
    assert cfg == {"project_id": PID, "url": "http://localhost:8080", "cookie_name": "overleaf.sid", "cookie_file": "/home/u/cookie"}
    assert tomllib.loads(render_config(PID, None, None, None)) == {"project_id": PID}


def test_gitignore_only_in_git_checkouts_and_only_adds_missing_lines(tmp_path):
    assert ensure_gitignore(tmp_path) == [] and not (tmp_path / ".gitignore").exists()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.aux")                  # no trailing newline
    added = ensure_gitignore(tmp_path)
    assert ".overleaf-sync-backup/" in added
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "*.aux" and ".overleaf-sync-base/" in lines
    assert ensure_gitignore(tmp_path) == []                       # idempotent


def test_project_local_config_makes_its_folder_the_synced_folder(tmp_path, monkeypatch):
    (tmp_path / ".overleaf-sync.toml").write_text(render_config(PID, None, None, None))
    sub = tmp_path / "chapters"
    sub.mkdir()
    monkeypatch.chdir(sub)                                        # run from a subfolder, like git
    cfg, path = load_config(None)
    assert path == tmp_path / ".overleaf-sync.toml"
    args = argparse.Namespace(project_id=None, output_dir=None, url=None, cookie_name=None, cookie_file=None,
                              dry_run=False, allow_wipe=False, allow_unknown_protocol=False,
                              no_tree_sync=False, prune_backups=None)
    st = resolve_settings(args, cfg, path)
    assert st["project_id"] == PID and Path(st["output_dir"]) == tmp_path


def fake_env(monkeypatch, check_rc=0):
    seen = {}
    monkeypatch.setattr(overleaf_sync, "load_overleaf_cookie", lambda **k: "COOKIE")
    monkeypatch.setattr(overleaf_sync, "run_check", lambda settings, cookie: seen.update(settings=settings, cookie=cookie) or check_rc)
    monkeypatch.setattr(overleaf_sync, "main", lambda argv=None: seen.update(started=argv))
    return seen


def test_init_uses_the_current_folder_and_writes_the_config(tmp_path, monkeypatch):
    seen = fake_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    run_init([f"https://www.overleaf.com/project/{PID}", "--yes", "--no-start"])
    assert seen["settings"]["output_dir"] == str(tmp_path) and seen["settings"]["project_id"] == PID
    assert tomllib.loads((tmp_path / ".overleaf-sync.toml").read_text()) == {"project_id": PID}
    assert "started" not in seen


def test_init_can_start_syncing(tmp_path, monkeypatch):
    seen = fake_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    run_init([PID, "--yes", "--start"])
    assert seen["started"] == ["--config", str(tmp_path / ".overleaf-sync.toml")]


def test_init_with_dir_and_custom_instance(tmp_path, monkeypatch):
    seen = fake_env(monkeypatch)
    target = tmp_path / "thesis"
    run_init([f"http://localhost:8080/project/{PID}", "--dir", str(target), "--yes", "--no-start", "--cookie-name", "overleaf.sid"])
    cfg = tomllib.loads((target / ".overleaf-sync.toml").read_text())
    assert cfg["url"] == "http://localhost:8080" and cfg["cookie_name"] == "overleaf.sid"


def test_init_failing_check_writes_nothing(tmp_path, monkeypatch):
    fake_env(monkeypatch, check_rc=1)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        run_init([PID, "--yes", "--no-start"])
    assert not (tmp_path / ".overleaf-sync.toml").exists()


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    fake_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".overleaf-sync.toml").write_text('project_id = "keep"\n')
    with pytest.raises(SystemExit):
        run_init([PID, "--yes", "--no-start"])
    assert "keep" in (tmp_path / ".overleaf-sync.toml").read_text()
    run_init([PID, "--yes", "--no-start", "--force"])
    assert PID in (tmp_path / ".overleaf-sync.toml").read_text()


def test_init_without_a_terminal_and_without_a_url_fails_clearly(tmp_path, monkeypatch):
    fake_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", open(os.devnull))
    with pytest.raises(SystemExit) as e:
        run_init([])
    assert "project URL" in str(e.value)


def test_init_asks_before_writing_and_no_means_no(tmp_path, monkeypatch):
    fake_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    class TTY:
        def isatty(self): return True
        def readline(self): return "n\n"
    monkeypatch.setattr("sys.stdin", TTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    with pytest.raises(SystemExit):
        run_init([PID])
    assert not (tmp_path / ".overleaf-sync.toml").exists()


def test_main_dispatches_init(monkeypatch):
    got = []
    monkeypatch.setattr(overleaf_sync, "run_init", lambda a: got.append(a))
    overleaf_sync.main(["init", "x", "--yes"])
    assert got == [["x", "--yes"]]
