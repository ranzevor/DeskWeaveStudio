"""通过独立 Engine 进程保存流程并恢复原请求结果。"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory

import deskweave
from deskweave.process import EngineProcess, PythonLaunch
from deskweave.transport.native import (
    Installation,
    InstalledFile,
    ProcessBinding,
    current_executable,
)
from deskweave.transport.protocol import ConnectionPolicy


def development_launch(extra_files=()):
    def entry(path):
        path = Path(path).resolve(strict=True)
        return InstalledFile(path, hashlib.sha256(path.read_bytes()).hexdigest())

    roots, files = set(), set()
    for name in (
        "deskweave",
        "jsonschema",
        "jsonschema_specifications",
        "referencing",
        "rfc8785",
        "attrs",
        "attr",
        "rpds",
        "typing_extensions",
    ):
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        if origin.name == "__init__.py":
            roots.add(origin.parent.parent)
            files.update(
                path
                for path in origin.parent.rglob("*")
                if path.is_file()
                and path.suffix
                in {
                    ".py",
                    ".json",
                    ".pyd",
                    ".so",
                    ".dll",
                }
            )
        else:
            roots.add(origin.parent)
            files.add(origin)
    metadata = distribution("deskweave")
    roots.add(Path(metadata.locate_file("")).resolve())
    files.update(
        Path(metadata.locate_file(path)).resolve()
        for path in metadata.files
        if ".dist-info/" in str(path)
    )
    package = Path(deskweave.__file__).parent
    contracts = package / "contracts"
    if not contracts.is_dir():
        contracts = package.parents[1] / "contracts"
    files.update(contracts.glob("*.json"))
    files.add(Path(__file__).resolve())
    files.update(Path(path).resolve() for path in extra_files)
    executable = entry(current_executable())
    launcher = entry(sys._base_executable)
    files.add(launcher.path)
    requirement = None
    if sys.platform == "darwin":
        import subprocess

        signed = subprocess.run(
            ["/usr/bin/codesign", "-d", "-r-", str(executable.path)],
            capture_output=True,
            text=True,
            check=True,
        )
        requirement = (signed.stdout + signed.stderr).split("designated => ", 1)[1].splitlines()[0]
    installation = Installation(
        executable, tuple(entry(path) for path in sorted(files)), requirement
    )
    return PythonLaunch(launcher, installation, tuple(sorted(roots)))


def main():
    launch = development_launch()
    definition = json.loads(
        (Path(__file__).parents[1] / "workflows/hello.json").read_text(encoding="utf-8")
    )
    with TemporaryDirectory(prefix="dw-process-") as temporary:
        with ProcessBinding(
            os.getpid(), launch.installation, instance_epoch="example-host"
        ) as host:
            process = EngineProcess(launch, host, Path(temporary) / "workspace", create=True)
            try:
                credential = process.authorize(
                    host, ConnectionPolicy({"workflow.read", "workflow.write"}), lifetime=60
                )
                with process.connect(credential, host) as client:
                    saved = client.call(
                        "workflow.save",
                        {
                            "workflowId": definition["id"],
                            "draftVersion": 1,
                            "baseRevision": None,
                            "definition": definition,
                            "dependencies": {},
                        },
                        request_id="process-save",
                    )
                    if not saved["ok"]:
                        raise RuntimeError(saved["error"])
                with process.connect(credential, host) as client:
                    recovered = client.call(
                        "request.status", {"requestId": "process-save"}, request_id="process-status"
                    )
                    if not recovered["ok"] or recovered["result"]["result"] != saved["result"]:
                        raise RuntimeError("原请求查询结果不一致。")
            finally:
                process.close()
                if process.wait() != 0:
                    raise RuntimeError(process.diagnostics)
    print("独立 Engine 进程保存、重连查询与资源关闭完成。")


if __name__ == "__main__":
    main()
