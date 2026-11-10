"""执行 Engine 测试；Linux CI 通过 PAM 创建真实登录会话。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main():
    arguments = sys.argv[1:] or ["discover", "-s", "tests", "-q"]
    examples = {
        "--example": "examples/engine/native_connection.py",
        "--process-example": "examples/engine/process.py",
        "--coordination-example": "examples/engine/coordination.py",
    }
    command = (
        [sys.executable, examples[arguments[0]]]
        if len(arguments) == 1 and arguments[0] in examples
        else [sys.executable, "-m", "unittest", *arguments]
    )
    if sys.platform == "linux" and os.environ.get("CI") == "true":
        command = [
            "sudo",
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            f"--uid={os.getuid()}",
            "--property=PAMName=login",
            f"--working-directory={Path.cwd()}",
            "--setenv=PYTHONUTF8=1",
            "--setenv=PYTHONIOENCODING=utf-8",
            *command,
        ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
