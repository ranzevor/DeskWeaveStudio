"""在临时工作空间上传、保存并分块读取大流程模型。"""

import base64
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.json import digest, dumps, loads


def main():
    definition = {
        "formatVersion": "1.0",
        "id": "large-model",
        "name": "分块传输示例",
        "entry": "result",
        "parameters": {},
        "variables": {"payload": ["x" * 200000 for _ in range(6)]},
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
                "title": "返回结果",
                "action": "flow.return",
                "params": {"values": {"completed": True}},
                "on": {},
            }
        ],
    }
    model = {"definition": definition, "dependencies": {}}
    raw = dumps(model, max_bytes=33554432)
    with TemporaryDirectory(prefix="deskweave-content-") as directory:
        with Workspace(Path(directory) / "workspace", create=True) as workspace:
            engine = Engine(workspace)
            session = engine.open_session(
                "content-example",
                {"workflow.read", "workflow.write"},
                capabilities={"contentRefsV1"},
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
                        "deadlineMs": 30000,
                        "params": params,
                    },
                )
                if not response["ok"]:
                    raise RuntimeError(response["error"]["message"])
                return response["result"]

            try:
                started = call(
                    "content.begin",
                    {
                        "purpose": "workflowModel",
                        "mediaType": "application/json",
                        "length": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                )
                for offset in range(0, len(raw), started["maxChunkBytes"]):
                    block = raw[offset : offset + started["maxChunkBytes"]]
                    written = call(
                        "content.write",
                        {
                            "uploadId": started["uploadId"],
                            "offset": offset,
                            "chunk": base64.b64encode(block).decode(),
                            "chunkSha256": hashlib.sha256(block).hexdigest(),
                        },
                    )
                    if written["nextOffset"] != offset + len(block):
                        raise RuntimeError("上传偏移不匹配。")
                sealed = call("content.seal", {"uploadId": started["uploadId"]})
                saved = call(
                    "workflow.save",
                    {
                        "workflowId": definition["id"],
                        "draftVersion": 1,
                        "baseRevision": None,
                        "modelRef": sealed["reference"],
                    },
                )
                call("content.discard", {"uploadId": started["uploadId"]})
                loaded = call(
                    "workflow.read",
                    {"workflowId": definition["id"], "revision": saved["revision"]},
                )
                resource = loaded["modelResource"]
                received = bytearray()
                chunks = 0
                while len(received) < resource["length"]:
                    page = call(
                        "resource.read",
                        {
                            "resourceId": resource["resourceId"],
                            "offset": len(received),
                            "length": started["maxChunkBytes"],
                        },
                    )
                    block = base64.b64decode(page["chunk"], validate=True)
                    if (
                        page["resourceId"] != resource["resourceId"]
                        or page["offset"] != len(received)
                        or page["length"] != len(block)
                        or page["totalLength"] != resource["length"]
                        or page["sha256"] != resource["sha256"]
                        or not block
                        or len(received) + len(block) > resource["length"]
                    ):
                        raise RuntimeError("资源块身份或长度不匹配。")
                    received.extend(block)
                    chunks += 1
                if hashlib.sha256(received).hexdigest() != resource["sha256"]:
                    raise RuntimeError("资源整体摘要不匹配。")
                restored = loads(bytes(received), max_bytes=33554432)
                if restored != model or digest(restored["definition"]) != saved["definitionHash"]:
                    raise RuntimeError("读取的流程与保存版本不一致。")
                call("content.discard", {"resourceId": resource["resourceId"]})
                print(
                    dumps(
                        {
                            "name": restored["definition"]["name"],
                            "bytes": len(received),
                            "chunks": chunks,
                        }
                    ).decode()
                )
            finally:
                engine.close()


if __name__ == "__main__":
    main()
