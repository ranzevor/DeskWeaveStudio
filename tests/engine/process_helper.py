from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from deskweave.process import EngineProcess, PythonLaunch
from deskweave.process import _wire as wire
from deskweave.transport.native import ProcessBinding
from deskweave.transport.protocol import ConnectionPolicy


def output(value):
    print(json.dumps(value), flush=True)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        os.set_inheritable(stream.fileno(), False)
    config = json.loads(sys.stdin.readline())
    reader = wire.ManifestReader(config["installation"])
    for offset in range(0, len(config["files"]), 128):
        reader.append(config["files"][offset : offset + 128])
    installation = reader.finish()
    launch = PythonLaunch(
        wire.parse_file(config["launcher"]),
        installation,
        tuple(Path(value) for value in config["paths"]),
    )
    with ExitStack() as stack:
        host = stack.enter_context(
            ProcessBinding(os.getpid(), installation, instance_epoch="helper-owner")
        )
        process = EngineProcess(launch, host, Path(config["workspace"]), create=True)
        stack.callback(process.wait)
        stack.callback(process.close)
        output(
            {
                "pid": process.pid,
                "peer": process.peer.as_dict(),
                "address": process.address,
                "workspace": process.workspace,
            }
        )
        for line in sys.stdin:
            command = json.loads(line)
            if command["action"] == "client":
                expected = wire.peer_value(command["peer"])
                peer = stack.enter_context(
                    ProcessBinding(
                        expected.process_id, installation, instance_epoch=expected.instance_epoch
                    )
                )
                if peer.identity != expected:
                    raise RuntimeError("fixture peer mismatch")
                credential = process.authorize(
                    peer, ConnectionPolicy({"workflow.read", "workflow.write"}), lifetime=60
                )
                output(wire.ticket_value(process.issue(credential)))
            elif command["action"] == "stop":
                break
            else:
                raise ValueError("fixture control method")


if __name__ == "__main__":
    main()
