"""Bounded unittest execution with a clean environment and process-tree cleanup."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def execute_tests(directory: Path, *, allowed=False, timeout=10, cancelled=None) -> dict:
    if not allowed:
        return {"status": "not_run", "passed": None, "tests_run": None, "output": "未开启测试执行。", "exit_code": None}
    environment = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "LANG", "LC_ALL"}}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    started, reason = time.monotonic(), ""
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen([sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test*.py", "-v"],
                                   cwd=directory, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=os.name != "nt")
        while process.poll() is None:
            if cancelled and cancelled.is_set():
                reason = "cancelled"
            elif time.monotonic() - started > timeout:
                reason = "timeout"
            elif output.tell() > 65536:
                reason = "output_limit"
            if reason:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                break
            time.sleep(.02)
        process.wait(timeout=5)
        output.seek(0)
        text = re.sub(r"\r+\n", "\n", output.read(65537).decode("utf-8", errors="replace"))
    count = re.search(r"^Ran (\d+) tests? in", text, re.M)
    tests_run = int(count.group(1)) if count else None
    if not reason and not tests_run:
        reason = "no_tests"
    return {"status": reason or ("passed" if process.returncode == 0 else "failed"),
            "passed": process.returncode == 0 and not reason, "tests_run": tests_run,
            "output": text[:65536], "exit_code": process.returncode,
            "duration": round(time.monotonic() - started, 3)}
