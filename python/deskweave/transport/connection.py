from __future__ import annotations

import copy
import secrets
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from deskweave.contracts import method_schema, validate
from deskweave.engine.service import Session
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME, dumps, loads
from deskweave.transport.protocol import (
    ConnectionProfile,
    ConnectionTicket,
    PeerIdentity,
    negotiate,
    packet,
    proof,
    transcript,
    verify,
)

if TYPE_CHECKING:
    from deskweave.transport.authority import ConnectionAuthority


class _Connection:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = "new"
        self._ticket: ConnectionTicket | None = None
        self._transcript: dict | None = None
        self._server_proof: str | None = None
        self._client_proof: str | None = None
        self._selected: dict | None = None
        self._max_frame_bytes = MAX_FRAME

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state == "closed"

    @property
    def selected(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._selected)

    def _expect(self, state: str) -> None:
        if self._state == "closed":
            raise DeskWeaveError("TRANSPORT_CLOSED", "消息连接已关闭。")
        if self._state != state:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接消息顺序无效。")

    def _clear_handshake(self) -> None:
        self._ticket = None
        self._transcript = None
        self._server_proof = None
        self._client_proof = None

    def _abort(self, original: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            raise BaseExceptionGroup("连接处理与关闭失败。", [original, cleanup]) from None

    def close(self) -> None:
        with self._lock:
            self._state = "closed"
            self._clear_handshake()

    def __enter__(self):
        return self

    def __exit__(self, _kind, error, _traceback):
        if error is None:
            self.close()
        else:
            self._abort(error)


class ServerConnection(_Connection):
    def __init__(
        self,
        authority: ConnectionAuthority,
        peer: PeerIdentity,
        *,
        check_peer: Callable[[], None] | None = None,
        close_transport: Callable[[], None] | None = None,
    ):
        super().__init__()
        self.authority = authority
        self._check_peer = check_peer
        self._close_transport = close_transport
        self.peer = peer
        self._session: Session | None = None
        self._max_frame_bytes = authority.profile.max_frame_bytes

    @property
    def session(self) -> Session | None:
        with self._lock:
            return self._session

    def _guard(self) -> None:
        if self._check_peer is not None:
            self._check_peer()
        session = self._session
        if session is None:
            raise DeskWeaveError("ACCESS_DENIED", "连接尚未完成认证。")
        self.authority.check(self, session.client_id, session.authorization_version)

    def receive(self, value: object) -> dict:
        with self._lock:
            try:
                if self._check_peer is not None:
                    self._check_peer()
                if self._state == "new":
                    return self._hello(value)
                if self._state == "challenge":
                    return self._authenticate(value)
                self._expect("ready")
                self._guard()
                request = loads(
                    dumps(value, max_bytes=self._max_frame_bytes, checkpoint=self._guard),
                    checkpoint=self._guard,
                )
                if type(request) is not dict or request.get("kind") != "request":
                    raise DeskWeaveError("INVALID_ARGUMENT", "认证后的连接需要业务请求。")
                response = self.authority.engine.dispatch(
                    self._session.id,
                    request,
                    authorize=self._guard,
                    max_frame_bytes=self._max_frame_bytes,
                )
                self._guard()
                dumps(response, max_bytes=self._max_frame_bytes, checkpoint=self._guard)
                return response
            except BaseException as error:
                self._abort(error)
                raise

    def _hello(self, value: object) -> dict:
        dumps(value, max_bytes=self._max_frame_bytes)
        hello = packet(value, "hello")
        ticket = self.authority.begin(self, hello["ticketId"])
        client = ConnectionProfile.from_dict(hello["peer"])
        if client.instance_epoch != self.peer.instance_epoch:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接实例与原生对端不匹配。")
        self._selected = negotiate(client, self.authority.profile)
        self._max_frame_bytes = self._selected["limits"]["maxFrameBytes"]
        challenge = {
            "kind": "connection.challenge",
            "nonce": secrets.token_hex(32),
            "peer": self.authority.profile.as_dict(),
            "selected": self._selected,
        }
        self._ticket = ticket
        self._transcript = transcript(ticket, hello, challenge)
        self._server_proof = proof(ticket.secret, "server", self._transcript)
        response = packet({**challenge, "proof": self._server_proof}, "challenge")
        dumps(response, max_bytes=self._max_frame_bytes)
        self._state = "challenge"
        return response

    def _authenticate(self, value: object) -> dict:
        dumps(value, max_bytes=self._max_frame_bytes)
        message = packet(value, "authenticate")
        ticket = self._ticket
        verify(
            message["proof"],
            proof(
                ticket.secret,
                "client",
                {
                    "transcript": self._transcript,
                    "serverProof": self._server_proof,
                },
            ),
        )
        grant = self.authority.consume(self, ticket)
        self._session = self.authority.engine.open_session(
            grant.client_id,
            grant.policy.permissions,
            local=grant.policy.local,
            workflow_ids=grant.policy.workflow_ids,
            capabilities=set(self._selected["capabilities"]),
            authorization_version=grant.version,
        )
        self._guard()
        ready = {
            "kind": "connection.ready",
            "session": self._session.id,
            "workspace": ticket.workspace,
            "clientId": grant.client_id,
            "authorizationVersion": grant.version,
            "selected": self._selected,
        }
        ready_proof = proof(
            ticket.secret,
            "ready",
            {
                "transcript": self._transcript,
                "clientProof": message["proof"],
                "ready": ready,
            },
        )
        response = packet({**ready, "proof": ready_proof}, "ready")
        dumps(response, max_bytes=self._max_frame_bytes, checkpoint=self._guard)
        self.authority.activate(self, grant.client_id, grant.version)
        self._state = "ready"
        self._clear_handshake()
        return response

    def close(self) -> None:
        failures = []
        if self._close_transport is not None:
            try:
                self._close_transport()
            except BaseException as error:
                failures.append(error)
        try:
            with self._lock:
                super().close()
                self.authority.release(self)
                if self._session is not None:
                    self.authority.engine.close_session(self._session.id)
                    self._session = None
                if not failures:
                    self.authority.finished_close(self)
        except BaseException as error:
            failures.append(error)
        if failures:
            if len(failures) == 1:
                raise failures[0]
            raise BaseExceptionGroup("连接资源关闭失败。", failures)


class ClientConnection(_Connection):
    def __init__(
        self,
        ticket: ConnectionTicket,
        client_peer: PeerIdentity,
        server_peer: PeerIdentity,
        *,
        max_frame_bytes: int = MAX_FRAME,
    ):
        super().__init__()
        if type(ticket) is not ConnectionTicket:
            raise TypeError("连接需要一次性票据。")
        if (
            type(client_peer) is not PeerIdentity
            or type(server_peer) is not PeerIdentity
            or ticket.client_peer != client_peer
            or ticket.server_peer != server_peer
        ):
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接票据与原生对端不匹配。")
        self._ticket = ticket
        self.profile = ConnectionProfile.current(client_peer, max_frame_bytes=max_frame_bytes)
        self._max_frame_bytes = max_frame_bytes
        self._hello: dict | None = None
        self._session: str | None = None
        self.workspace = ticket.workspace
        self.client_id = ticket.client_id
        self.authorization_version = ticket.authorization_version

    @property
    def session(self) -> str | None:
        with self._lock:
            return self._session

    def start(self) -> dict:
        with self._lock:
            try:
                self._expect("new")
                self._hello = {
                    "kind": "connection.hello",
                    "ticketId": self._ticket.ticket_id,
                    "nonce": secrets.token_hex(32),
                    "peer": self.profile.as_dict(),
                }
                result = packet(self._hello, "hello")
                dumps(result, max_bytes=self._max_frame_bytes)
                self._state = "hello"
                return result
            except BaseException as error:
                self._abort(error)
                raise

    def receive(self, value: object) -> dict | None:
        with self._lock:
            try:
                dumps(value, max_bytes=self._max_frame_bytes)
                if self._state == "hello":
                    return self._challenge(value)
                self._expect("authenticate")
                ready = packet(value, "ready")
                if (
                    ready["workspace"] != self.workspace
                    or ready["clientId"] != self.client_id
                    or ready["authorizationVersion"] != self.authorization_version
                    or ready["selected"] != self._selected
                ):
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "连接会话与票据不匹配。")
                verify(
                    ready["proof"],
                    proof(
                        self._ticket.secret,
                        "ready",
                        {
                            "transcript": self._transcript,
                            "clientProof": self._client_proof,
                            "ready": {key: item for key, item in ready.items() if key != "proof"},
                        },
                    ),
                )
                self._session = ready["session"]
                self._state = "ready"
                self._clear_handshake()
                self._hello = None
                return None
            except BaseException as error:
                self._abort(error)
                raise

    def _challenge(self, value: object) -> dict:
        message = packet(value, "challenge")
        server = ConnectionProfile.from_dict(message["peer"])
        if server.instance_epoch != self._ticket.server_peer.instance_epoch:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接实例与原生对端不匹配。")
        selected = negotiate(self.profile, server)
        if message["selected"] != selected:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接协商结果不匹配。")
        self._transcript = transcript(
            self._ticket,
            self._hello,
            {key: item for key, item in message.items() if key != "proof"},
        )
        verify(message["proof"], proof(self._ticket.secret, "server", self._transcript))
        self._selected = selected
        self._max_frame_bytes = selected["limits"]["maxFrameBytes"]
        dumps(message, max_bytes=self._max_frame_bytes)
        self._client_proof = proof(
            self._ticket.secret,
            "client",
            {
                "transcript": self._transcript,
                "serverProof": message["proof"],
            },
        )
        self._state = "authenticate"
        return packet(
            {"kind": "connection.authenticate", "proof": self._client_proof}, "authenticate"
        )

    def request(
        self,
        method: str,
        params: dict,
        *,
        request_id: str | None = None,
        deadline_ms: int = 30000,
    ) -> dict:
        with self._lock:
            self._expect("ready")
            value = loads(
                dumps(
                    {
                        "protocolVersion": "1.0",
                        "kind": "request",
                        "id": request_id or secrets.token_hex(16),
                        "session": self._session,
                        "workspace": self.workspace,
                        "method": method,
                        "params": params,
                        "deadlineMs": deadline_ms,
                    },
                    max_bytes=self._max_frame_bytes,
                )
            )
            validate("urn:deskweave:message:1#/$defs/requestEnvelope", value)
            return value

    def response(self, value: object, *, request: dict) -> dict:
        with self._lock:
            try:
                self._expect("ready")
                original = loads(dumps(request, max_bytes=self._max_frame_bytes))
                validate("urn:deskweave:message:1#/$defs/requestEnvelope", original)
                result = loads(dumps(value, max_bytes=self._max_frame_bytes))
                validate("urn:deskweave:message:1", result)
                if (
                    original["session"] != self._session
                    or original["workspace"] != self.workspace
                    or result["kind"] != "response"
                    or result["replyTo"] != original["id"]
                ):
                    raise DeskWeaveError("INVALID_ARGUMENT", "响应与原请求不匹配。")
                if result["ok"]:
                    validate(method_schema(original["method"], "result"), result["result"])
                return result
            except BaseException as error:
                self._abort(error)
                raise

    def close(self) -> None:
        with self._lock:
            super().close()
            self._hello = None
            self._session = None
