"""Backup pruning is opt-in, removes only old timestamped runs, and never the current one."""
import datetime

from overleaf_sync import OverleafSync


def runs(tmp_path, names):
    root = tmp_path / ".overleaf-sync-backup"
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "deleted.jsonl").write_text("x")
    return root


def stamp(days_ago):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")


def test_default_keeps_everything(tmp_path):
    root = runs(tmp_path, [stamp(400)])
    OverleafSync("pid", "c", str(tmp_path))._prune_backups()
    assert len(list(root.iterdir())) == 1


def test_old_runs_go_recent_and_foreign_stay(tmp_path):
    old, recent = stamp(40), stamp(2)
    root = runs(tmp_path, [old, recent, "notes-by-me"])
    sync = OverleafSync("pid", "c", str(tmp_path), prune_backups_days=30)
    sync._prune_backups()
    assert sorted(p.name for p in root.iterdir()) == sorted([recent, "notes-by-me"])


def test_the_current_run_is_never_pruned(tmp_path):
    sync = OverleafSync("pid", "c", str(tmp_path), prune_backups_days=0.0001)
    sync._started = stamp(40)
    root = runs(tmp_path, [sync._started])
    sync._prune_backups()
    assert len(list(root.iterdir())) == 1


def test_missing_backup_folder_is_fine(tmp_path):
    OverleafSync("pid", "c", str(tmp_path), prune_backups_days=1)._prune_backups()
