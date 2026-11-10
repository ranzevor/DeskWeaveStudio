"""多个宿主会话共用工作空间 Engine，并在空闲后退出。"""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__:
    from .process import development_launch
else:
    from process import development_launch

from deskweave.coordination import StartupAuthority, WorkspaceCoordinator, WorkspaceIdentity
from deskweave.transport.native import ProcessBinding
from deskweave.transport.protocol import ConnectionPolicy


def main():
    launch = development_launch()
    with ExitStack() as stack:
        root = Path(stack.enter_context(TemporaryDirectory(prefix="dw-coordination-")))
        local = stack.enter_context(
            ProcessBinding(
                os.getpid(),
                launch.installation,
                instance_epoch="coordination-example",
            )
        )
        identity = stack.enter_context(WorkspaceIdentity(root / "workspace", create=True))
        authority = StartupAuthority(identity, launch.installation)
        grant = authority.register_host(
            local,
            ConnectionPolicy({"workflow.read"}),
            lifetime=60,
            manager=True,
        )
        plan = authority.seal()
        coordinator = stack.enter_context(
            WorkspaceCoordinator(
                launch,
                local,
                directory=root / "runtime",
                idle_grace=0.1,
            )
        )
        first = coordinator.open(identity, grant, plan=plan, create=True)
        process = first.control
        try:
            with coordinator.open(identity, grant, create=True) as second:
                if first.peer != second.peer:
                    raise RuntimeError("工作空间实例不一致。")
                first.close()
                with second.connect() as client:
                    result = client.call(
                        "request.status",
                        {"requestId": "example-request"},
                        request_id="query",
                    )
                    if not result["ok"]:
                        raise RuntimeError(result["error"])
        finally:
            coordinator.close()
            if process.wait() != 0:
                raise RuntimeError(process.diagnostics)
    print("共享 Engine 接入、会话关闭后的查询和空闲退出完成。")


if __name__ == "__main__":
    main()
