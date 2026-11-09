"""在临时工作空间通过本机原生通道保存流程并查询结果。"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import deskweave
from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.native import (
    Installation,
    InstalledFile,
    NativeClient,
    NativeListener,
    ProcessBinding,
    connect,
    current_executable,
    serve_connection,
)
from deskweave.transport.protocol import ConnectionPolicy


def development_installation():
    def entry(path):
        path = Path(path).resolve(strict=True)
        return InstalledFile(path, hashlib.sha256(path.read_bytes()).hexdigest())

    executable = entry(current_executable())
    components = tuple(
        entry(path)
        for path in Path(deskweave.__file__).parent.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json"}
    )
    requirement = None
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/codesign", "-d", "-r-", str(executable.path)],
            capture_output=True,
            text=True,
            check=True,
        )
        requirement = (result.stdout + result.stderr).split("designated => ", 1)[1].splitlines()[0]
    return Installation(executable, components, requirement)


def main():
    definition = json.loads(
        (Path(__file__).parents[1] / "workflows/hello.json").read_text(encoding="utf-8")
    )
    installation = development_installation()
    with TemporaryDirectory(prefix="dw-example-") as temporary:
        engine = Engine(Workspace(Path(temporary) / "workspace", create=True))
        try:
            with ProcessBinding(
                os.getpid(), installation, instance_epoch="native-example"
            ) as binding:
                with ConnectionAuthority(engine, binding.identity) as authority:
                    credential = authority.authorize(
                        ConnectionPolicy({"workflow.read", "workflow.write"}), lifetime=60
                    )
                    with NativeListener(binding, directory=temporary) as listener:
                        tickets = queue.Queue()

                        def serve():
                            with listener.accept({os.getpid(): binding}) as channel:
                                tickets.put(authority.issue(credential, channel.peer))
                                try:
                                    serve_connection(channel, authority)
                                except DeskWeaveError as error:
                                    if error.code != "TRANSPORT_CLOSED":
                                        raise

                        with ThreadPoolExecutor(max_workers=1) as workers:
                            saved = None
                            for attempt in range(2):
                                worker = workers.submit(serve)
                                with connect(listener.address, binding, binding) as channel:
                                    with NativeClient(channel, tickets.get(timeout=10)) as client:
                                        if attempt == 0:
                                            response = client.call(
                                                "workflow.save",
                                                {
                                                    "workflowId": definition["id"],
                                                    "draftVersion": 1,
                                                    "baseRevision": None,
                                                    "definition": definition,
                                                    "dependencies": {},
                                                },
                                                request_id="native-example-save",
                                            )
                                            if not response["ok"]:
                                                raise RuntimeError(response["error"])
                                            saved = response["result"]
                                        else:
                                            response = client.call(
                                                "request.status",
                                                {"requestId": "native-example-save"},
                                                request_id="native-example-status",
                                            )
                                            if (
                                                not response["ok"]
                                                or response["result"]["result"] != saved
                                            ):
                                                raise RuntimeError("原请求查询结果不一致。")
                                worker.result(timeout=10)
            print("原生通道认证、流程保存与重连查询完成。")
        finally:
            engine.close()


if __name__ == "__main__":
    main()
