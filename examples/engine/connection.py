"""使用合成对端身份和临时工作空间演示连接认证及保存恢复。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from deskweave.adapters.framing import FrameDecoder, encode_frame
from deskweave.adapters.workspace import Workspace
from deskweave.engine.service import Engine
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.connection import ClientConnection
from deskweave.transport.protocol import ConnectionPolicy, PeerIdentity


def framed(value):
    decoder = FrameDecoder()
    encoded = encode_frame(value)
    messages = decoder.feed(encoded[:3]) + decoder.feed(encoded[3:])
    decoder.finish()
    return messages[0]


def connect(authority, credential, client_peer):
    ticket = authority.issue(credential, client_peer)
    server = authority.accept(client_peer)
    client = ClientConnection(ticket, client_peer, authority.identity)
    try:
        challenge = server.receive(framed(client.start()))
        ready = server.receive(framed(client.receive(framed(challenge))))
        client.receive(framed(ready))
    except BaseException:
        try:
            client.close()
        finally:
            server.close()
        raise
    return client, server


def call(client, server, method, params, request_id):
    request = client.request(method, params, request_id=request_id)
    response = client.response(framed(server.receive(framed(request))), request=request)
    if not response["ok"]:
        raise RuntimeError(response["error"])
    return response["result"]


def main():
    server_peer = PeerIdentity(
        "windows", "example-user", "example-login", 1, "start-1", "a" * 64, "engine-1"
    )
    client_peer = PeerIdentity(
        "windows", "example-user", "example-login", 2, "start-2", "b" * 64, "studio-1"
    )
    definition = json.loads(
        (Path(__file__).parents[1] / "workflows/hello.json").read_text(encoding="utf-8")
    )
    with TemporaryDirectory() as temporary:
        engine = Engine(Workspace(Path(temporary) / "workspace", create=True))
        try:
            with ConnectionAuthority(engine, server_peer) as authority:
                credential = authority.authorize(
                    ConnectionPolicy({"workflow.read", "workflow.write"}, local=True), lifetime=300
                )
                client, server = connect(authority, credential, client_peer)
                with client, server:
                    saved = call(
                        client,
                        server,
                        "workflow.save",
                        {
                            "workflowId": definition["id"],
                            "draftVersion": 1,
                            "baseRevision": None,
                            "definition": definition,
                            "dependencies": {},
                        },
                        "save-example",
                    )
                client, server = connect(authority, credential, client_peer)
                with client, server:
                    recovered = call(
                        client,
                        server,
                        "request.status",
                        {"requestId": "save-example"},
                        "query-save",
                    )
                    if recovered["result"] != saved:
                        raise RuntimeError("恢复后的保存结果不一致。")
                print("连接认证、流程保存与重连查询完成。")
        finally:
            engine.close()


if __name__ == "__main__":
    main()
