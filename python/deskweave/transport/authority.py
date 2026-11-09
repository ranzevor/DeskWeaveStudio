from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from deskweave.engine.service import Engine
from deskweave.errors import DeskWeaveError
from deskweave.json import MAX_FRAME
from deskweave.transport.protocol import (
    ClientCredential,
    ConnectionPolicy,
    ConnectionProfile,
    ConnectionTicket,
    PeerIdentity,
)

if TYPE_CHECKING:
    from deskweave.transport.connection import ServerConnection


@dataclass
class _Grant:
    credential_digest: bytes
    policy: ConnectionPolicy
    expires_at: float
    version: int = 1


@dataclass(frozen=True)
class GrantSnapshot:
    client_id: str
    policy: ConnectionPolicy
    expires_at: float
    version: int


@dataclass
class _Ticket:
    value: ConnectionTicket
    expires_at: float


@dataclass
class _Registration:
    client_id: str | None
    deadline: float | None


class ConnectionAuthority:
    def __init__(
        self,
        engine: Engine,
        identity: PeerIdentity,
        *,
        max_frame_bytes: int = MAX_FRAME,
        max_clients: int = 256,
        max_tickets: int = 256,
        max_connections: int = 64,
        ticket_lifetime: float = 60.0,
        handshake_lifetime: float = 10.0,
    ):
        if type(identity) is not PeerIdentity:
            raise TypeError("连接端点需要原生对端身份。")
        for count in (max_clients, max_tickets, max_connections):
            if type(count) is not int or not 1 <= count <= 4096:
                raise ValueError("连接登记容量应为 1～4096。")
        self._lifetime(ticket_lifetime)
        self._lifetime(handshake_lifetime)
        if ticket_lifetime > 60 or handshake_lifetime > 10:
            raise ValueError("票据和握手有效期超过连接协议上限。")
        self.engine = engine
        self.identity = identity
        self.profile = ConnectionProfile.current(identity, max_frame_bytes=max_frame_bytes)
        self.max_clients = max_clients
        self.max_tickets = max_tickets
        self.max_connections = max_connections
        self.ticket_lifetime = ticket_lifetime
        self.handshake_lifetime = handshake_lifetime
        self._lock = threading.RLock()
        self._clients: dict[str, _Grant] = {}
        self._tickets: dict[str, _Ticket] = {}
        self._connections: dict[ServerConnection, _Registration] = {}
        self._closing: set[ServerConnection] = set()
        self._closed = False

    @staticmethod
    def _lifetime(value: float) -> None:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("授权有效期必须为有限正数。")

    def now(self) -> float:
        return self.engine.monotonic()

    def _expiry(self, lifetime: float) -> float:
        self._lifetime(lifetime)
        expiry = self.now() + lifetime
        if not math.isfinite(expiry):
            raise ValueError("授权到期时间超出范围。")
        return expiry

    def _open_locked(self) -> None:
        if self._closed or self.engine.workspace.closed:
            raise DeskWeaveError("TRANSPORT_CLOSED", "连接服务已关闭。")

    def _peer(self, peer: PeerIdentity) -> None:
        if type(peer) is not PeerIdentity:
            raise TypeError("连接端点需要原生对端身份。")
        if (peer.platform, peer.user_id, peer.os_session_id) != (
            self.identity.platform,
            self.identity.user_id,
            self.identity.os_session_id,
        ):
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接对端不属于当前登录会话。")

    def _credential_locked(self, credential: ClientCredential) -> _Grant:
        self._open_locked()
        if type(credential) is not ClientCredential:
            raise DeskWeaveError("ACCESS_DENIED", "身份恢复凭据无效。")
        grant = self._clients.get(credential.client_id)
        if grant is None or not hmac.compare_digest(
            grant.credential_digest, hashlib.sha256(credential.secret).digest()
        ):
            raise DeskWeaveError("ACCESS_DENIED", "身份恢复凭据无效。")
        if grant.expires_at <= self.now():
            raise DeskWeaveError("ACCESS_DENIED", "连接授权已到期。")
        return grant

    @staticmethod
    def _close_all(connections: list[ServerConnection]) -> None:
        failures = []
        for connection in connections:
            try:
                connection.close()
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("连接资源关闭失败。", failures)

    def _remove_client_connections_locked(self, client_id: str) -> list[ServerConnection]:
        selected = [
            connection
            for connection, registration in self._connections.items()
            if registration.client_id == client_id
        ]
        for connection in selected:
            del self._connections[connection]
            self._closing.add(connection)
        return selected

    def sweep(self) -> None:
        with self._lock:
            now = self.now()
            expired = {key for key, grant in self._clients.items() if grant.expires_at <= now}
            for key in expired:
                del self._clients[key]
            self._tickets = {
                key: ticket
                for key, ticket in self._tickets.items()
                if ticket.expires_at > now and ticket.value.client_id not in expired
            }
            selected = [
                connection
                for connection, registration in self._connections.items()
                if registration.client_id in expired
                or registration.deadline is not None
                and registration.deadline <= now
            ]
            for connection in selected:
                del self._connections[connection]
                self._closing.add(connection)
        self._close_all(list(set(selected) | self._closing_snapshot()))

    def authorize(self, policy: ConnectionPolicy, *, lifetime: float) -> ClientCredential:
        if type(policy) is not ConnectionPolicy:
            raise TypeError("授权登记需要完整的连接策略。")
        self._lifetime(lifetime)
        self.sweep()
        with self._lock:
            self._open_locked()
            if len(self._clients) >= self.max_clients:
                raise DeskWeaveError("LIMIT_EXCEEDED", "客户端授权登记数量已满。")
            client_id = secrets.token_urlsafe(24)
            while client_id in self._clients:
                client_id = secrets.token_urlsafe(24)
            credential = ClientCredential(client_id, secrets.token_bytes(32))
            self._clients[client_id] = _Grant(
                hashlib.sha256(credential.secret).digest(), policy, self._expiry(lifetime)
            )
            return credential

    def describe(self, credential: ClientCredential) -> GrantSnapshot:
        with self._lock:
            grant = self._credential_locked(credential)
            return GrantSnapshot(
                credential.client_id, grant.policy, grant.expires_at, grant.version
            )

    def authorization_version(self, credential: ClientCredential) -> int:
        with self._lock:
            return self._credential_locked(credential).version

    def revise(
        self,
        credential: ClientCredential,
        policy: ConnectionPolicy,
        *,
        expected_version: int,
        lifetime: float,
    ) -> int:
        if type(policy) is not ConnectionPolicy:
            raise TypeError("授权登记需要完整的连接策略。")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("授权版本无效。")
        self._lifetime(lifetime)
        with self._lock:
            grant = self._credential_locked(credential)
            if grant.version != expected_version:
                raise DeskWeaveError("AUTHORIZATION_CONFLICT", "连接授权已更新。")
            if grant.version == 9007199254740991:
                raise DeskWeaveError("LIMIT_EXCEEDED", "连接授权版本已达到上限。")
            grant.policy, grant.expires_at = policy, self._expiry(lifetime)
            grant.version += 1
            version = grant.version
            self._tickets = {
                key: value
                for key, value in self._tickets.items()
                if value.value.client_id != credential.client_id
            }
            selected = self._remove_client_connections_locked(credential.client_id)
        self._close_all(selected)
        return version

    def revoke(self, credential: ClientCredential) -> None:
        with self._lock:
            self._open_locked()
            if type(credential) is not ClientCredential:
                raise DeskWeaveError("ACCESS_DENIED", "身份恢复凭据无效。")
            grant = self._clients.get(credential.client_id)
            if grant is None:
                return
            if not hmac.compare_digest(
                grant.credential_digest, hashlib.sha256(credential.secret).digest()
            ):
                raise DeskWeaveError("ACCESS_DENIED", "身份恢复凭据无效。")
            del self._clients[credential.client_id]
            self._tickets = {
                key: value
                for key, value in self._tickets.items()
                if value.value.client_id != credential.client_id
            }
            selected = self._remove_client_connections_locked(credential.client_id)
        self._close_all(selected)

    def issue(self, credential: ClientCredential, peer: PeerIdentity) -> ConnectionTicket:
        self._peer(peer)
        self.sweep()
        with self._lock:
            grant = self._credential_locked(credential)
            if len(self._tickets) >= self.max_tickets:
                raise DeskWeaveError("LIMIT_EXCEEDED", "连接票据数量已满。")
            identity = secrets.token_urlsafe(24)
            while identity in self._tickets:
                identity = secrets.token_urlsafe(24)
            value = ConnectionTicket(
                identity,
                secrets.token_bytes(32),
                credential.client_id,
                grant.version,
                self.engine.workspace.id,
                peer,
                self.identity,
            )
            self._tickets[identity] = _Ticket(
                value, min(grant.expires_at, self.now() + self.ticket_lifetime)
            )
            return value

    def accept(
        self,
        peer: PeerIdentity,
        *,
        check_peer: Callable[[], None] | None = None,
        close_transport: Callable[[], None] | None = None,
    ) -> ServerConnection:
        from deskweave.transport.connection import ServerConnection

        self._peer(peer)
        self.sweep()
        with self._lock:
            self._open_locked()
            if len(self._connections) + len(self._closing) >= self.max_connections:
                raise DeskWeaveError("LIMIT_EXCEEDED", "连接数量已满。")
            connection = ServerConnection(
                self, peer, check_peer=check_peer, close_transport=close_transport
            )
            self._connections[connection] = _Registration(
                None, self.now() + self.handshake_lifetime
            )
            return connection

    def _registration_locked(self, connection: ServerConnection) -> _Registration:
        self._open_locked()
        registration = self._connections.get(connection)
        if registration is None:
            raise DeskWeaveError("ACCESS_DENIED", "连接授权已失效。")
        if registration.deadline is not None and registration.deadline <= self.now():
            raise DeskWeaveError("AUTHENTICATION_FAILED", "连接握手已到期。")
        return registration

    def begin(self, connection: ServerConnection, ticket_id: str) -> ConnectionTicket:
        with self._lock:
            registration = self._registration_locked(connection)
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.expires_at <= self.now():
                raise DeskWeaveError("AUTHENTICATION_FAILED", "连接票据无效或已到期。")
            value = ticket.value
            grant = self._clients.get(value.client_id)
            if (
                grant is None
                or grant.version != value.authorization_version
                or grant.expires_at <= self.now()
                or value.client_peer != connection.peer
                or value.server_peer != self.identity
                or value.workspace != self.engine.workspace.id
            ):
                raise DeskWeaveError("AUTHENTICATION_FAILED", "连接票据与对端或授权不匹配。")
            registration.client_id = value.client_id
            registration.deadline = min(registration.deadline, ticket.expires_at)
            return value

    def consume(self, connection: ServerConnection, ticket: ConnectionTicket) -> GrantSnapshot:
        with self._lock:
            self._registration_locked(connection)
            current = self._tickets.get(ticket.ticket_id)
            if current is None or current.value != ticket or current.expires_at <= self.now():
                raise DeskWeaveError("AUTHENTICATION_FAILED", "连接票据无效或已到期。")
            self.check(connection, ticket.client_id, ticket.authorization_version)
            grant = self._clients[ticket.client_id]
            del self._tickets[ticket.ticket_id]
            return GrantSnapshot(ticket.client_id, grant.policy, grant.expires_at, grant.version)

    def check(self, connection: ServerConnection, client_id: str, version: int) -> None:
        with self._lock:
            registration = self._registration_locked(connection)
            grant = self._clients.get(client_id)
            if (
                registration.client_id != client_id
                or grant is None
                or grant.version != version
                or grant.expires_at <= self.now()
            ):
                raise DeskWeaveError("ACCESS_DENIED", "连接授权已失效。")

    def activate(self, connection: ServerConnection, client_id: str, version: int) -> None:
        with self._lock:
            self.check(connection, client_id, version)
            self._connections[connection].deadline = None

    def release(self, connection: ServerConnection) -> None:
        with self._lock:
            self._connections.pop(connection, None)
            self._closing.add(connection)

    def finished_close(self, connection: ServerConnection) -> None:
        with self._lock:
            self._closing.discard(connection)

    def _closing_snapshot(self) -> set[ServerConnection]:
        with self._lock:
            return set(self._closing)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            selected = list(set(self._connections) | self._closing)
            self._connections.clear()
            self._closing.update(selected)
            self._clients.clear()
            self._tickets.clear()
        self._close_all(selected)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
