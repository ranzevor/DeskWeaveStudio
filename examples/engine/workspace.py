"""在临时工作空间中保存、读取和校验流程。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.json import dumps


def main():
    definition = {
        "formatVersion": "1.0",
        "id": "welcome",
        "name": "问候流程",
        "entry": "result",
        "parameters": {},
        "variables": {},
        "targets": {},
        "resources": {},
        "limits": {
            "activeMs": 1000,
            "steps": 10,
            "callDepth": 1,
            "parallelCalls": 1,
            "inputEvents": 1,
        },
        "nodes": [
            {
                "id": "result",
                "title": "返回问候",
                "action": "flow.return",
                "params": {"values": {"message": "你好，DeskWeaveStudio"}},
                "on": {},
            }
        ],
    }
    with TemporaryDirectory(prefix="deskweave-example-") as directory:
        with Workspace(Path(directory) / "workspace", create=True) as workspace:
            engine = Engine(workspace)
            session = engine.open_session(
                "example", {"workflow.read", "workflow.write"}, local=True
            )

            def call(method, params):
                response = engine.dispatch(
                    session.id,
                    {
                        "protocolVersion": "1.0",
                        "kind": "request",
                        "id": uuid4().hex,
                        "session": session.id,
                        "workspace": workspace.id,
                        "method": method,
                        "deadlineMs": 5000,
                        "params": params,
                    },
                )
                if not response["ok"]:
                    raise RuntimeError(response["error"]["message"])
                return response["result"]

            saved = call(
                "workflow.save",
                {
                    "workflowId": "welcome",
                    "draftVersion": 1,
                    "baseRevision": None,
                    "definition": definition,
                    "dependencies": {},
                },
            )
            loaded = call("workflow.read", {"workflowId": "welcome", "revision": saved["revision"]})
            checked = call(
                "workflow.validate", {"workflowId": "welcome", "revision": saved["revision"]}
            )
            preview = call(
                "expression.preview", {"value": {"$expr": {"op": "add", "args": [2, 3]}}}
            )
            print(
                dumps(
                    {
                        "name": loaded["definition"]["name"],
                        "valid": checked["valid"],
                        "preview": preview["value"],
                    }
                ).decode()
            )
            engine.close()


if __name__ == "__main__":
    main()
