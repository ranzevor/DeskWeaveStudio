from __future__ import annotations

import secrets

from deskweave.errors import DeskWeaveError
from deskweave.process import _wire as wire

from .authority import proof, verify_proof


def binding_digest(binding, checkpoint=lambda: None):
    checkpoint()
    return binding.digest


def nonce(value):
    if (
        type(value) is not str
        or len(value) != 64
        or any(letter not in "0123456789abcdef" for letter in value)
    ):
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主认证随机数无效。")
    return value


def transcript(binding, client_nonce, server_nonce, workspace, server):
    return {
        "binding": binding_digest(binding),
        "clientNonce": client_nonce,
        "serverNonce": server_nonce,
        "workspace": workspace,
        "server": server.as_dict(),
        "client": binding.peer.as_dict(),
    }


def client(channel, grant, budget):
    if channel.local != grant.binding.peer:
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主授权与当前进程不匹配。")
    if grant.binding.epoch is not None and channel.peer.instance_epoch != grant.binding.epoch:
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主授权的执行服务实例已改变。")
    key = grant.verification_key
    client_nonce = secrets.token_hex(32)
    channel.send(
        {
            "kind": "host.hello",
            "authority": grant.binding.authority,
            "grantId": grant.binding.grant_id,
            "nonce": client_nonce,
        },
        budget=budget,
    )
    challenge = channel.receive(budget=budget)
    if challenge.get("kind") != "host.challenge":
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主接入认证失败。")
    wire.fields(challenge, ("kind", "nonce", "workspace", "binding", "proof"))
    server_nonce = nonce(challenge["nonce"])
    if (
        challenge["binding"] != binding_digest(grant.binding, budget.check_time)
        or type(challenge["workspace"]) is not str
        or not 1 <= len(challenge["workspace"]) <= 128
    ):
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主授权绑定不匹配。")
    if grant.binding.workspace is not None and grant.binding.workspace != challenge["workspace"]:
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主授权的工作空间不匹配。")
    value = transcript(
        grant.binding, client_nonce, server_nonce, challenge["workspace"], channel.peer
    )
    expected = proof(key, "server", value, budget.check_time)
    verify_proof(challenge["proof"], expected)
    authentication = proof(
        key, "client", {"transcript": value, "serverProof": expected}, budget.check_time
    )
    channel.send({"kind": "host.authenticate", "proof": authentication}, budget=budget)
    ready = channel.receive(budget=budget)
    wire.fields(ready, ("kind", "workspace", "address", "proof"))
    if (
        ready["kind"] != "host.ready"
        or ready["workspace"] != challenge["workspace"]
        or type(ready["address"]) is not str
        or not 1 <= len(ready["address"]) <= 4096
    ):
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主就绪响应无效。")
    body = {name: value for name, value in ready.items() if name != "proof"}
    verify_proof(
        ready["proof"],
        proof(
            key,
            "ready",
            {"transcript": value, "clientProof": authentication, "ready": body},
            budget.check_time,
        ),
    )
    budget.check_time()
    return ready


def server(channel, record, hello, workspace, address, budget, admit):
    wire.fields(hello, ("kind", "authority", "grantId", "nonce"))
    binding, key = record.binding, record.key
    if (
        hello["kind"] != "host.hello"
        or hello["authority"] != binding.authority
        or hello["grantId"] != binding.grant_id
        or channel.peer != binding.peer
    ):
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主接入认证失败。")
    client_nonce = nonce(hello["nonce"])
    server_nonce = secrets.token_hex(32)
    value = transcript(binding, client_nonce, server_nonce, workspace, channel.local)
    server_proof = proof(key, "server", value, budget.check_time)
    channel.send(
        {
            "kind": "host.challenge",
            "nonce": server_nonce,
            "workspace": workspace,
            "binding": binding_digest(binding, budget.check_time),
            "proof": server_proof,
        },
        budget=budget,
    )
    response = channel.receive(budget=budget)
    wire.fields(response, ("kind", "proof"))
    if response["kind"] != "host.authenticate":
        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主接入认证失败。")
    expected = proof(
        key, "client", {"transcript": value, "serverProof": server_proof}, budget.check_time
    )
    verify_proof(response["proof"], expected)
    admit()
    ready = {"kind": "host.ready", "workspace": workspace, "address": address}
    ready["proof"] = proof(
        key,
        "ready",
        {"transcript": value, "clientProof": expected, "ready": dict(ready)},
        budget.check_time,
    )
    channel.send(ready, budget=budget)
