from __future__ import annotations

import base64
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from deskweave.coordination import HostGrant, StartupPlan, WorkspaceCoordinator, WorkspaceIdentity
from deskweave.coordination.authority import HostBinding, HostRegistration
from deskweave.coordination.startup import Discovery
from deskweave.process import PythonLaunch
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
        local = stack.enter_context(
            ProcessBinding(os.getpid(), installation, instance_epoch=config["epoch"])
        )
        identity = stack.enter_context(WorkspaceIdentity(config["workspace"], create=True))
        coordinator = stack.enter_context(
            WorkspaceCoordinator(launch, local, directory=config["runtime"], idle_grace=0.3)
        )
        output(local.identity.as_dict())
        session = None
        for line in sys.stdin:
            command = json.loads(line)
            action = command["action"]
            if action == "open":
                grant = HostGrant.from_dict(command["grant"])
                plan = None
                if command.get("hosts") is not None:
                    plan = StartupPlan(
                        grant.binding.authority,
                        grant.binding.directory,
                        grant.binding.workspace,
                        grant.binding.engine_installation,
                        tuple(
                            HostRegistration(
                                HostBinding.from_dict(item["binding"]),
                                installation,
                                base64.b64decode(item["key"], validate=True),
                            )
                            for item in command["hosts"]
                        ),
                    )
                publish = Discovery.publish

                def advertise(*args, publish=publish, command=command):
                    publish(*args)
                    if command.get("exitAfterPublish"):
                        os._exit(0)

                with patch.object(Discovery, "publish", advertise):
                    session = coordinator.open(identity, grant, plan=plan, create=True)
                output(
                    {
                        "peer": session.peer.as_dict(),
                        "workspace": session.workspace,
                        "created": session.created,
                    }
                )
            elif action == "query":
                with session.connect() as client:
                    output(
                        client.call("request.status", {"requestId": "missing"}, request_id="query")
                    )
            elif action == "register":
                expected = wire.peer_value(command["peer"])
                with ProcessBinding(
                    expected.process_id, installation, instance_epoch=expected.instance_epoch
                ) as peer:
                    if peer.identity != expected:
                        raise RuntimeError("fixture peer mismatch")
                    grant = session.register_host(
                        peer,
                        ConnectionPolicy({"workflow.read"}),
                        lifetime=60,
                        manager=command.get("manager", False),
                    )
                output(grant.as_dict())
            elif action == "close":
                session.close()
                output({})
            elif action == "stop":
                break
            else:
                raise ValueError("fixture control method")


if __name__ == "__main__":
    main()
