from __future__ import annotations

import heapq
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field

from deskweave.errors import DeskWeaveError
from deskweave.process import _wire as wire
from deskweave.process._worker import Admission, Runtime
from deskweave.transport.native import NativeListener
from deskweave.transport.native._common import Budget, InitializationError, cleanup

from . import handshake
from .authority import MAX_HOSTS, HostBinding, HostGrant, policy_from, within
from .startup import HOST_BYTES, Discovery


@dataclass
class HostRecord:
    binding: HostBinding
    key: bytes = field(repr=False)
    credential: object = field(repr=False)
    size: int = 0
    parent: str | None = None
    pending: bool = True
    departed: bool = False
    default_client: object = field(default=None, repr=False)
    channels: set = field(default_factory=set)
    children: set = field(default_factory=set)
    clients: set = field(default_factory=set)


@dataclass
class ControlState:
    host: HostRecord
    channel: object
    pending: object = None
    kind: str | None = None
    new_manager: bool = False
    reserved_states: int = 0
    reserved_bytes: int = 0
    reserved_metadata: int = 0
    other_states: int = 0
    other_manifest_bytes: int = 0
    not_after: float | None = None
    stop: bool = False


class Diagnostics:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = bytearray()

    def write(self, text):
        data = str(text).encode("utf-8", errors="replace")
        with self.lock:
            self.data.extend(data[-65536:])
            if len(self.data) > 65536:
                del self.data[:-65536]
        return len(text)

    def flush(self):
        pass

    def read(self):
        with self.lock:
            return bytes(self.data).decode("utf-8", errors="replace")


class SharedRuntime(Runtime):
    def __init__(
        self,
        listener,
        authority,
        parent,
        *,
        max_states,
        max_bytes,
        identity,
        runtime_directory,
        plan,
        config,
    ):
        super().__init__(listener, authority, parent, max_states=max_states, max_bytes=max_bytes)
        self.guard = threading.RLock()
        self.identity, self.plan, self.config = identity, plan, config
        self.discovery = Discovery(identity, runtime_directory)
        self.hosts, self.owners = {}, {}
        self.host_expirations, self.hold_expirations = [], []
        self.host_bytes = self.pending_hosts = self.pending_count = 0
        self.pending_states = self.pending_bytes = self.pending_metadata = 0
        self.host_threads, self.host_channels = set(), set()
        self.closing_channels = set()
        self.failed_closes = {}
        self.host_capacity = threading.BoundedSemaphore(MAX_HOSTS)
        self.visible = threading.Event()
        self.published = threading.Event()
        self.host_listener = self.host_acceptor = None
        self.root = None
        self.diagnostics = Diagnostics()
        self.cleanup_reported = False
        self.registry.on_forget = self.forget_client
        try:
            for item in plan.hosts:
                if item.binding.expires_at <= authority.now():
                    continue
                try:
                    credential = self.registry.add(
                        item.binding.peer,
                        item.installation,
                        item.binding.policy,
                        item.binding.expires_at - authority.now(),
                        Budget(max(config["deadline"] - time.monotonic(), 1e-9)),
                        not_after=item.binding.expires_at,
                    )
                except DeskWeaveError as error:
                    if error.code in {"AUTHENTICATION_FAILED", "ACCESS_DENIED"}:
                        continue
                    raise
                record = self.remember_host(item.binding, item.verification_key, credential)
                if item.binding.peer == parent.identity and item.binding.manager:
                    self.root = record
            if self.root is None:
                raise DeskWeaveError("ACCESS_DENIED", "启动宿主没有有效的管理授权。")
            self.host_listener = NativeListener(listener.local)
        except BaseException as error:
            self._abort_initialization(error)
            raise

    def remember_host(self, binding, key, credential, parent=None, *, reservation=0):
        size = len(wire.dumps(binding.as_dict())) + 96
        if (
            len(self.hosts) >= MAX_HOSTS
            or self.host_bytes + self.pending_metadata - reservation + size > HOST_BYTES
        ):
            self.registry.revoke(credential)
            raise DeskWeaveError("LIMIT_EXCEEDED", "宿主登记容量已满。")
        record = HostRecord(binding, key, credential, size=size, parent=parent)
        self.hosts[binding.grant_id] = record
        self.host_bytes += size
        self.pending_count += 1
        heapq.heappush(self.host_expirations, (binding.expires_at, binding.grant_id))
        heapq.heappush(
            self.hold_expirations,
            (min(binding.expires_at, self.authority.now() + 30), binding.grant_id),
        )
        if parent is not None:
            self.hosts[parent].children.add(binding.grant_id)
        return record

    def forget_client(self, client):
        owner = self.owners.pop(client, None)
        record = self.hosts.get(owner)
        if record is not None:
            record.clients.discard(client)
            if record.default_client is not None and record.default_client.client_id == client:
                record.default_client = None

    def checked_host(self, record):
        if (
            self.hosts.get(record.binding.grant_id) is not record
            or record.departed
            or record.binding.expires_at <= self.authority.now()
        ):
            raise DeskWeaveError("ACCESS_DENIED", "宿主授权已结束。")
        self.authority.describe(record.credential)

    def claimed(self, record, channel):
        self.checked_host(record)
        if record.pending:
            record.pending = False
            self.pending_count -= 1
        record.channels.add(channel)

    def observe_publication(self):
        if self.visible.is_set():
            return True
        try:
            value = self.discovery.read()
        except (DeskWeaveError, OSError, ValueError):
            return False
        if (
            value is not None
            and value["peer"] == self.listener.local.identity
            and value["workspace"] == self.authority.engine.workspace.id
            and value["authority"] == self.plan.authority
            and value["control"] == self.host_listener.address
            and value["address"] == self.listener.address
        ):
            self.identity.check(value["workspace"])
            self.visible.set()
        return self.visible.is_set()

    def publish(self):
        if self.cancel.is_set() or not self.observe_publication():
            raise DeskWeaveError("ACCESS_DENIED", "执行服务定位记录尚未发布。")
        self.published.set()
        return {
            "workspace": self.authority.engine.workspace.id,
            "peer": self.listener.local.identity.as_dict(),
        }

    def drop_host(self, record):
        self.hosts.pop(record.binding.grant_id, None)
        self.host_bytes -= record.size
        parent = self.hosts.get(record.parent)
        if parent is not None:
            parent.children.discard(record.binding.grant_id)
            if parent.departed and not parent.children:
                self.drop_host(parent)

    def retire_hosts(self, records, *, defer=None):
        actions, clients = [], set()
        for record in records:
            record.departed = True
            if record.pending:
                record.pending = False
                self.pending_count -= 1
            for channel in tuple(record.channels):
                if channel is not defer:
                    self.closing_channels.add(channel)
                    actions.append(channel.close)
            clients.update(record.clients)
            clients.add(record.credential.client_id)
        cleanup(lambda: self.registry.revoke_clients(clients), *actions)

    def revoke_host(self, identity, *, defer=None):
        queue, selected = [identity], []
        while queue:
            record = self.hosts.get(queue.pop())
            if record is None:
                continue
            queue.extend(record.children)
            selected.append(record)
        for record in reversed(selected):
            if record.binding.grant_id in self.hosts:
                self.drop_host(record)
        self.retire_hosts(selected, defer=defer)

    def depart_host(self, record):
        try:
            self.retire_hosts((record,))
        finally:
            if not record.children and record.binding.grant_id in self.hosts:
                self.drop_host(record)

    def sweep_hosts(self):
        errors = []
        try:
            errors.extend(self.registry.prune())
        except BaseExceptionGroup as error:
            errors.append(error)
        now = self.authority.now()
        while self.hold_expirations and self.hold_expirations[0][0] <= now:
            _, identity = heapq.heappop(self.hold_expirations)
            record = self.hosts.get(identity)
            if record is not None and record.pending:
                record.pending = False
                self.pending_count -= 1
        while self.host_expirations and self.host_expirations[0][0] <= now:
            _, identity = heapq.heappop(self.host_expirations)
            try:
                self.revoke_host(identity)
            except BaseExceptionGroup as error:
                errors.append(error)
        for record in tuple(self.hosts.values()):
            if record.departed:
                continue
            peer = self.registry.get(record.binding.peer.process_id)
            try:
                if peer is None:
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主进程登记已结束。")
                peer._check_process()
            except DeskWeaveError as error:
                if error.code != "AUTHENTICATION_FAILED":
                    raise
                try:
                    self.depart_host(record)
                except BaseExceptionGroup as closing:
                    errors.append(closing)
        if len(self.host_expirations) > 2 * len(self.hosts) + 32:
            self.host_expirations = [
                (item.binding.expires_at, identity) for identity, item in self.hosts.items()
            ]
            heapq.heapify(self.host_expirations)
        if len(self.hold_expirations) > 2 * len(self.hosts) + 32:
            self.hold_expirations = [
                (deadline, identity)
                for deadline, identity in self.hold_expirations
                if identity in self.hosts and self.hosts[identity].pending
            ]
            heapq.heapify(self.hold_expirations)
        return errors

    def clear_state(self, state):
        with self.guard:
            self.pending_states -= state.reserved_states
            self.pending_bytes -= state.reserved_bytes
            self.pending_metadata -= state.reserved_metadata
            if state.kind == "host":
                self.pending_hosts -= 1
            state.pending = state.kind = None
            state.new_manager = False
            state.reserved_states = state.reserved_bytes = state.reserved_metadata = 0

    def dispatch_control(self, state, method, params, budget):
        budget.acquire(self.guard)
        try:
            self.checked_host(state.host)
            if state.pending is not None:
                prefix = "host." if state.kind == "host" else ""
                if method not in {prefix + "register.files", prefix + "register.commit"}:
                    raise DeskWeaveError("INVALID_ARGUMENT", "宿主登记分块尚未结束。")
            if method == "host.publish":
                wire.fields(params, ())
                return self.publish()
            if method == "host.client":
                wire.fields(params, ())
                if not self.published.is_set():
                    raise DeskWeaveError("ACCESS_DENIED", "执行服务尚未发布。")
                record = state.host
                if record.default_client is None:
                    grant = self.registry.selected(record.credential)
                    credential = self.registry.add(
                        record.binding.peer,
                        grant.peer.binding.installation,
                        record.binding.policy,
                        record.binding.expires_at - self.authority.now(),
                        budget,
                        not_after=record.binding.expires_at,
                    )
                    record.default_client = credential
                    record.clients.add(credential.client_id)
                    self.owners[credential.client_id] = record.binding.grant_id
                return wire.credential_value(record.default_client)
            if method == "host.revoke":
                wire.fields(params, ("grantId",))
                if not state.host.binding.manager:
                    raise DeskWeaveError("ACCESS_DENIED", "当前宿主没有管理权限。")
                target = self.hosts.get(params["grantId"])
                ancestor = target
                while ancestor is not None and ancestor is not state.host:
                    ancestor = self.hosts.get(ancestor.parent)
                if target is not None and ancestor is None:
                    raise DeskWeaveError("ACCESS_DENIED", "宿主撤销超出委托范围。")
                try:
                    self.revoke_host(params["grantId"], defer=state.channel)
                finally:
                    state.stop = state.host.binding.grant_id not in self.hosts
                return {}
            if method == "host.diagnostics":
                wire.fields(params, ())
                if not state.host.binding.manager:
                    raise DeskWeaveError("ACCESS_DENIED", "当前宿主没有管理权限。")
                return {"text": self.diagnostics.read()}
            host_registration = method.startswith("host.register.")
            selected = method[5:] if host_registration else method
            if host_registration and not state.host.binding.manager:
                raise DeskWeaveError("ACCESS_DENIED", "当前宿主没有登记权限。")
            if selected.startswith("register.") and not self.published.is_set():
                raise DeskWeaveError("ACCESS_DENIED", "执行服务尚未发布。")
            if selected == "register.begin":
                if host_registration:
                    wire.fields(params, ("peer", "installation", "policy", "lifetime", "manager"))
                    if type(params["manager"]) is not bool:
                        raise ValueError("宿主管理角色无效。")
                    if len(self.hosts) + self.pending_hosts >= MAX_HOSTS:
                        raise DeskWeaveError("LIMIT_EXCEEDED", "宿主登记数量已满。")
                    state.new_manager = params["manager"]
                    params = {key: value for key, value in params.items() if key != "manager"}
                requested = wire.peer_value(params["peer"])
                selected_policy = policy_from(params["policy"])
                if not within(selected_policy, state.host.binding.policy) or (
                    not state.host.binding.manager and requested != state.host.binding.peer
                ):
                    raise DeskWeaveError("ACCESS_DENIED", "客户端登记超出宿主授权。")
                metadata = len(wire.dumps(params, checkpoint=budget.check_time)) + 2048
                if self.host_bytes + self.pending_metadata + metadata > HOST_BYTES:
                    raise DeskWeaveError("LIMIT_EXCEEDED", "控制事务元数据额度已满。")
                state.other_states = self.pending_states
                state.other_manifest_bytes = self.pending_bytes
                state.not_after = state.host.binding.expires_at
                result = super().dispatch(selected, params, budget, state)
                state.kind = "host" if host_registration else "client"
                if host_registration:
                    self.pending_hosts += 1
                state.reserved_metadata = metadata
                self.pending_metadata += metadata
                _, reader, installation, _, _ = state.pending
                known = self.registry.known.get(requested.process_id)
                state.reserved_states = (
                    0
                    if requested.process_id in self.registry.peers
                    or known is not None
                    and known.identity == requested
                    else wire.header_count(params["installation"]) + 1
                )
                self.pending_states += state.reserved_states
                state.reserved_bytes = reader.bytes if reader is not None else 0
                self.pending_bytes += state.reserved_bytes
                return result
            if selected in {"register.files", "register.commit"}:
                if state.pending is None or host_registration != (state.kind == "host"):
                    raise DeskWeaveError("INVALID_ARGUMENT", "登记事务类型不匹配。")
                reader = state.pending[1]
                if reader is not None:
                    reader.byte_limit = (
                        self.registry.max_bytes
                        - self.registry.bytes
                        - self.pending_bytes
                        + state.reserved_bytes
                    )
                try:
                    result = super().dispatch(selected, params, budget, state)
                finally:
                    actual = reader.bytes if reader is not None else 0
                    self.pending_bytes += actual - state.reserved_bytes
                    state.reserved_bytes = actual
                if selected == "register.commit":
                    credential = wire.credential(result)
                    try:
                        if host_registration:
                            registered = self.registry.selected(credential)
                            binding = HostBinding(
                                self.plan.authority,
                                secrets.token_urlsafe(24),
                                self.identity.object,
                                self.authority.engine.workspace.id,
                                self.listener.local.identity.installation_digest,
                                registered.peer.binding.identity,
                                self.authority.describe(credential).policy,
                                registered.deadline,
                                state.new_manager,
                                self.listener.local.identity.instance_epoch,
                            )
                            grant = HostGrant(binding, credential.secret)
                            self.remember_host(
                                binding,
                                grant.verification_key,
                                credential,
                                state.host.binding.grant_id,
                                reservation=state.reserved_metadata,
                            )
                            result = grant.as_dict()
                        else:
                            self.owners[credential.client_id] = state.host.binding.grant_id
                            state.host.clients.add(credential.client_id)
                    except BaseException as error:
                        try:
                            self.registry.revoke(credential)
                        except BaseException as closing:
                            raise BaseExceptionGroup(
                                "宿主登记与清理失败。", [error, closing]
                            ) from None
                        raise
                    self.clear_state(state)
                return result
            if selected in {"ticket", "revise", "revoke"}:
                credential = wire.credential(params["credential"])
                if self.owners.get(credential.client_id) != state.host.binding.grant_id:
                    if selected == "revoke" and credential.client_id not in self.registry.grants:
                        wire.fields(params, ("credential",))
                        return {}
                    raise DeskWeaveError("ACCESS_DENIED", "客户端不属于当前宿主。")
                if selected == "revise" and not within(
                    policy_from(params["policy"]), state.host.binding.policy
                ):
                    raise DeskWeaveError("ACCESS_DENIED", "权限更新超出宿主授权。")
                state.not_after = state.host.binding.expires_at
            result = super().dispatch(selected, params, budget, state)
            if selected == "status":
                result.update(
                    {
                        "hosts": len(self.hosts),
                        "published": self.published.is_set(),
                        "pendingHosts": self.pending_count,
                    }
                )
            return result
        except BaseException:
            state.stop = state.host.binding.grant_id not in self.hosts
            self.clear_state(state)
            raise
        finally:
            self.guard.release()

    def start(self):
        if self.acceptor is not None:
            return

        def business():
            while not self.cancel.is_set() and not self.published.wait(0.1):
                pass
            if not self.cancel.is_set():
                self.accept()

        self.acceptor = threading.Thread(target=business, name="deskweave-business-admission")
        self.host_acceptor = threading.Thread(
            target=self.accept_hosts, name="deskweave-host-admission"
        )
        self.acceptor.start()
        self.host_acceptor.start()

    def accept_hosts(self):
        while not self.cancel.is_set():
            if not self.visible.wait(0.1) or not self.host_capacity.acquire(timeout=0.1):
                continue
            admission = Admission(self.registry)
            transferred = False
            channel = None
            try:
                channel = self.host_listener.accept(admission, timeout=10, cancel=self.cancel)
                self.start_host(channel, admission)
                transferred = True
            except DeskWeaveError as error:
                if error.code not in {
                    "DEADLINE_EXCEEDED",
                    "CANCELLED",
                    "AUTHENTICATION_FAILED",
                    "TRANSPORT_CLOSED",
                }:
                    self.record_failure(error)
            except InitializationError as error:
                channel = error.resource
                self.record_failure(error)
            except BaseException as error:
                if not self.cancel.is_set():
                    self.record_failure(error)
                    self.cancel.set()
            finally:
                if not transferred:
                    if channel is None:
                        admission.close()
                        self.host_capacity.release()
                    else:
                        self.finish_host(channel, None, admission)

    def start_host(self, channel, admission, root=None):
        thread = threading.Thread(
            target=self.serve_host, args=(channel, admission, root), name="deskweave-host"
        )
        with self.condition:
            self.host_threads.add(thread)
            self.host_channels.add(channel)
        try:
            thread.start()
        except BaseException:
            with self.condition:
                self.host_threads.discard(thread)
                self.host_channels.discard(channel)
            raise

    def finish_host(self, channel, record, admission):
        with self.guard:
            self.failed_closes[channel] = (record, admission)
            self.closing_channels.add(channel)
            with self.condition:
                self.host_channels.add(channel)
            return self.retry_controls()

    def serve_host(self, channel, admission, root=None):
        state = None
        record = root
        try:
            if record is None:
                budget = Budget(10, self.cancel)
                hello = channel.receive(budget=budget)
                if type(hello.get("grantId")) is not str:
                    raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主接入认证失败。")
                with self.guard:
                    record = self.hosts.get(hello.get("grantId"))
                    if record is None:
                        raise DeskWeaveError("AUTHENTICATION_FAILED", "宿主接入认证失败。")
                    self.checked_host(record)

                def admit():
                    with self.guard:
                        self.checked_host(record)
                        self.publish()
                        self.claimed(record, channel)

                handshake.server(
                    channel,
                    record,
                    hello,
                    self.authority.engine.workspace.id,
                    self.listener.address,
                    budget,
                    admit,
                )
            else:
                with self.guard:
                    self.claimed(record, channel)
            state = ControlState(record, channel)
            self.control_messages(
                channel,
                state,
                lambda method, params, budget: self.dispatch_control(state, method, params, budget),
            )
        except DeskWeaveError:
            pass
        except BaseException as error:
            self.record_failure(error)
        finally:
            if state is not None:
                self.clear_state(state)
            self.finish_host(channel, record, admission)
            with self.condition:
                self.host_threads.discard(threading.current_thread())
                self.condition.notify_all()

    def retry_controls(self):
        errors = []
        for channel in tuple(self.closing_channels):
            try:
                channel.close()
            except BaseException as error:
                errors.append(error)
                continue
            selected = self.failed_closes.pop(channel, None)
            if selected is not None:
                record, admission = selected
                if record is not None:
                    record.channels.discard(channel)
                admission.close()
                with self.condition:
                    self.host_channels.discard(channel)
                self.closing_channels.discard(channel)
                self.host_capacity.release()
        return errors

    def root_response(self):
        return {
            "workspace": self.authority.engine.workspace.id,
            "control": self.host_listener.address,
            "authority": self.plan.authority,
        }

    def run(self, control):
        old_stderr = sys.stderr
        sys.stderr = self.diagnostics
        old_stderr.close()
        self.start()
        admission = Admission(self.registry)
        if admission.get(self.root.binding.peer.process_id) is None:
            raise DeskWeaveError("AUTHENTICATION_FAILED", "启动宿主登记已失效。")
        self.host_capacity.acquire()
        try:
            self.start_host(control, admission, self.root)
        except BaseException:
            self.finish_host(control, self.root, admission)
            raise
        idle_since = None
        try:
            while not self.cancel.wait(0.1):
                with self.guard:
                    errors = self.sweep_hosts()
                    errors.extend(self.retry_controls())
                    errors.extend(self.retry_connections())
                    if errors and not self.cleanup_reported:
                        self.record_failure(BaseExceptionGroup("宿主资源释放失败。", errors))
                    self.cleanup_reported = bool(errors)
                    self.observe_publication()
                    now = self.authority.now()
                    if not self.published.is_set() and time.monotonic() >= self.config["deadline"]:
                        break
                    active = self.registry.active_users or self.pending_count
                    if active:
                        idle_since = None
                    elif idle_since is None:
                        idle_since = now
                    elif (
                        now - idle_since >= self.config["idleGrace"]
                        and self.registry.stop_when_idle()
                    ):
                        break
        finally:
            self.close()
        return 1 if self.failures else 0

    def close(self):
        self.cancel.set()
        self.visible.set()
        actions = []
        if self.host_listener is not None:
            actions.append(self.host_listener.close)

        def join_hosts():
            if self.host_acceptor is not None and self.host_acceptor.ident is not None:
                self.host_acceptor.join()
            with self.condition:
                channels = tuple(self.host_channels)
            errors = []
            for channel in channels:
                try:
                    channel.close()
                except BaseException as error:
                    errors.append(error)
                    with self.guard:
                        self.closing_channels.add(channel)
            while True:
                with self.condition:
                    if not self.host_threads:
                        break
                    self.condition.wait(0.1)
                with self.guard:
                    self.retry_controls()
            with self.guard:
                errors.extend(self.retry_controls())
            if errors:
                raise BaseExceptionGroup("宿主连接关闭失败。", errors)

        actions.extend((join_hosts, super().close))
        cleanup(*actions)
        if self.host_listener is not None:
            try:
                with self.identity.lock(Budget(0.1)):
                    self.discovery.remove(self.listener.local.identity)
            except DeskWeaveError as error:
                if error.code not in {"DEADLINE_EXCEEDED", "RESOURCE_DENIED", "TRANSPORT_CLOSED"}:
                    raise
