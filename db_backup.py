"""
Persistence for Render's free tier, which wipes local disk on every idle
restart: a dedicated private GitHub repo used purely as blob storage for
dynasty.db. Deliberately NOT the code repo Render deploys from -- pushing a
data snapshot there must never trigger a redeploy.

Everything happens inside a throwaway tempfile.mkdtemp() directory, entirely
outside this repo's own .git, so it can never touch this repo's history.

No-ops everywhere unless GITHUB_TOKEN / GITHUB_DATA_REPO_OWNER /
GITHUB_DATA_REPO_NAME are all set, so local dev needs zero config.
"""
import os
import shutil
import subprocess
import tempfile

import db

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_DATA_REPO_OWNER = os.environ.get("GITHUB_DATA_REPO_OWNER")
GITHUB_DATA_REPO_NAME = os.environ.get("GITHUB_DATA_REPO_NAME")

_GIT_TIMEOUT = 30  # seconds -- never let a flaky network hang startup/backup


def _enabled():
    return bool(GITHUB_TOKEN and GITHUB_DATA_REPO_OWNER and GITHUB_DATA_REPO_NAME)


def _remote_url():
    return f"https://{GITHUB_TOKEN}@github.com/{GITHUB_DATA_REPO_OWNER}/{GITHUB_DATA_REPO_NAME}.git"


def _redact(text):
    return text.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN and text else text


def _run(args, cwd=None):
    """subprocess.run wrapper that never lets the token leak into an
    exception message or log line."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    if result.returncode != 0:
        safe_args = [_redact(a) for a in args]
        raise RuntimeError(f"git command failed: {' '.join(safe_args)}\n{_redact(result.stderr)}")
    return result


def restore():
    """Pull the last dynasty.db snapshot down before db.init_db() runs.
    Safe no-op if disabled, if the data repo is empty (first-ever run), or
    if anything goes wrong -- a missing snapshot just means ingest starts
    from a blank DB, which is always correct, just slower to warm up."""
    if not _enabled():
        return False
    tmp = tempfile.mkdtemp(prefix="dynasty-gm-restore-")
    try:
        _run(["git", "clone", "--depth", "1", _remote_url(), tmp])
        src = os.path.join(tmp, "dynasty.db")
        if os.path.exists(src):
            shutil.copy2(src, db.DB_PATH)
            return True
        return False
    except Exception as exc:
        print(f"db_backup.restore(): {exc}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def backup():
    """Push the current dynasty.db up as the new (only) snapshot. Orphan
    commit + force-push every time so the data repo never accumulates
    history from repeatedly committing a binary file -- always exactly
    one commit."""
    if not _enabled():
        return False
    if not os.path.exists(db.DB_PATH):
        return False
    tmp = tempfile.mkdtemp(prefix="dynasty-gm-backup-")
    try:
        shutil.copy2(db.DB_PATH, os.path.join(tmp, "dynasty.db"))
        _run(["git", "init"], cwd=tmp)
        _run(["git", "checkout", "--orphan", "snapshot"], cwd=tmp)
        _run(["git", "add", "dynasty.db"], cwd=tmp)
        _run([
            "git", "-c", "user.email=dynasty-gm@bot.local", "-c", "user.name=dynasty-gm-bot",
            "commit", "-m", "snapshot",
        ], cwd=tmp)
        _run(["git", "remote", "add", "origin", _remote_url()], cwd=tmp)
        _run(["git", "push", "--force", "origin", "snapshot:main"], cwd=tmp)
        return True
    except Exception as exc:
        print(f"db_backup.backup(): {exc}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
