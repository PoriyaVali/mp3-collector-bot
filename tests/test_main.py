import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CRASH_DURING_STARTUP = """
import asyncio, app.main as m

async def boom(*args, **kwargs):
    raise RuntimeError("simulated crash")

m.serve = boom
raise SystemExit(asyncio.run(m.run()))
"""


def test_startup_crash_exits_instead_of_hanging(tmp_path):
    """A crash must end the process (so Docker restarts it), not leave it hung on the DB thread."""
    env = dict(os.environ, API_ID="1", API_HASH="0" * 32, BOT_TOKEN="1:" + "a" * 35,
               ADMIN_IDS="1", DATA_DIR=str(tmp_path), LISTEN_PORT="0")
    proc = subprocess.run([sys.executable, "-c", CRASH_DURING_STARTUP], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1
    assert "simulated crash" in proc.stderr
    assert (tmp_path / "bot.db").exists()


def test_missing_config_is_reported():
    env = {k: v for k, v in os.environ.items() if k not in ("API_ID", "API_HASH")}
    proc = subprocess.run([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "API_ID and API_HASH are required" in proc.stderr
