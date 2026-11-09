"""独立进程原生通道测试目标。控制材料仅通过父进程继承管道传入。"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.native import (
    Installation,
    InstalledFile,
    NativeListener,
    ProcessBinding,
    serve_connection,
)
from deskweave.transport.protocol import ConnectionPolicy


def output(value):
    print(json.dumps(value), flush=True)


def main():
    os.set_inheritable(sys.stdin.fileno(), False)
    os.set_inheritable(sys.stdout.fileno(), False)
    config = json.loads(sys.stdin.readline())
    policy = Installation(
        InstalledFile(Path(config["executable"][0]), config["executable"][1]),
        tuple(InstalledFile(Path(path), digest) for path, digest in config["components"]),
        config["requirement"],
    )
    with ExitStack() as stack:
        local = stack.enter_context(
            ProcessBinding(os.getpid(), policy, instance_epoch="engine-test")
        )
        peer = stack.enter_context(
            ProcessBinding(config["parent"], policy, instance_epoch="client-test")
        )
        advance = [0.0]
        engine = Engine(
            Workspace(Path(config["workspace"]), create=True),
            monotonic=lambda: time.monotonic() + advance[0],
        )
        stack.callback(engine.close)
        authority = stack.enter_context(ConnectionAuthority(engine, local.identity))
        credential = authority.authorize(
            ConnectionPolicy({"workflow.read", "workflow.write"}, local=True), lifetime=300
        )
        listener = stack.enter_context(NativeListener(local, directory=config["directory"]))
        output({"address": listener.address, "pid": os.getpid()})
        stop = threading.Event()
        channels, workers, errors = [], [], []

        def serve(channel):
            try:
                serve_connection(channel, authority, cancel=stop)
            except DeskWeaveError as error:
                errors.append(error.code)
            except BaseException as error:
                errors.append(type(error).__name__)

        try:
            for line in sys.stdin:
                command = json.loads(line)
                if command["action"] == "accept":
                    channel = listener.accept({config["parent"]: peer}, timeout=10)
                    channels.append(channel)
                    ticket = authority.issue(credential, channel.peer)
                    output(
                        {
                            "ticket": ticket.ticket_id,
                            "secret": base64.b64encode(ticket.secret).decode(),
                            "client": ticket.client_id,
                            "version": ticket.authorization_version,
                            "workspace": ticket.workspace,
                        }
                    )
                    worker = threading.Thread(target=serve, args=(channel,))
                    workers.append(worker)
                    worker.start()
                elif command["action"] == "expire":
                    advance[0] += 301
                    output({"expired": True})
                elif command["action"] == "revoke":
                    authority.revoke(credential)
                    output({"revoked": True})
                elif command["action"] == "join":
                    for worker in workers:
                        worker.join(5)
                    output(
                        {"active": sum(worker.is_alive() for worker in workers), "errors": errors}
                    )
                elif command["action"] == "stop":
                    break
                else:
                    raise ValueError("未知控制命令。")
        finally:
            stop.set()
            for channel in channels:
                channel.close()
            for worker in workers:
                worker.join(5)
            if any(worker.is_alive() for worker in workers):
                raise RuntimeError("连接线程没有退出。")


if __name__ == "__main__":
    main()
