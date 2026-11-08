import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from deskweave import contracts
from deskweave.adapters.framing import FrameDecoder, encode_frame
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from deskweave.transport.authority import ConnectionAuthority
from deskweave.transport.connection import ClientConnection
from deskweave.transport.protocol import (
    ClientCredential,
    ConnectionPolicy,
    ConnectionProfile,
    PeerIdentity,
    negotiate,
)
from tests.engine.test_service import EngineCase, workflow


def peer(process_id=1, platform="windows"):
    return PeerIdentity(
        platform, "user", "login", process_id, "started-1", "a" * 64, str(process_id)
    )


def wire(value):
    encoded = encode_frame(value)
    decoder = FrameDecoder()
    result = []
    for part in (encoded[:2], encoded[2:7], encoded[7:]):
        result.extend(decoder.feed(part))
    decoder.finish()
    if len(result) != 1:
        raise AssertionError("expected one decoded frame")
    return result[0]


class ConnectionTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.authority = ConnectionAuthority(self.engine, peer())
        self.addCleanup(self.authority.close)
        self.credential = self.authority.authorize(
            ConnectionPolicy({"workflow.read", "workflow.write"}, local=True), lifetime=300
        )

    def connect(self, *, credential=None, ticket=None, max_frame_bytes=1048576):
        ticket = ticket or self.authority.issue(credential or self.credential, peer(2))
        client = ClientConnection(ticket, peer(2), peer(), max_frame_bytes=max_frame_bytes)
        server = self.authority.accept(peer(2))
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        challenge = server.receive(wire(client.start()))
        authenticate = client.receive(wire(challenge))
        ready = server.receive(wire(authenticate))
        self.assertIsNone(client.receive(wire(ready)))
        self.assertEqual(client.session, server.session.id)
        return client, server

    def call(self, pair, method, params, **kwargs):
        client, server = pair
        request = client.request(method, params, **kwargs)
        return client.response(wire(server.receive(wire(request))), request=request)

    def params(self):
        return {
            "workflowId": "hello",
            "draftVersion": 1,
            "baseRevision": None,
            "definition": workflow(),
            "dependencies": {},
        }

    def assert_code(self, code, action):
        with self.assertRaises(DeskWeaveError) as error:
            action()
        self.assertEqual(error.exception.code, code)

    def test_framed_save_and_reconnect_preserve_request_owner(self):
        pair = self.connect()
        saved = self.call(pair, "workflow.save", self.params(), request_id="save-once")
        self.assertTrue(saved["ok"], saved)
        first_session = pair[0].session
        pair[1].close()
        pair[0].close()
        restored = self.connect()
        self.assertNotEqual(first_session, restored[0].session)
        status = self.call(restored, "request.status", {"requestId": "save-once"})
        self.assertEqual(status["result"]["result"], saved["result"])
        repeated = self.call(restored, "workflow.save", self.params(), request_id="save-once")
        self.assertEqual(repeated, saved)
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM revision").fetchone()[0], 1)

    def test_platform_identities_and_wrong_native_peer(self):
        for platform in ("windows", "macos", "linux"):
            with self.subTest(platform=platform):
                with ConnectionAuthority(self.engine, peer(platform=platform)) as authority:
                    credential = authority.authorize(ConnectionPolicy(set()), lifetime=30)
                    ticket = authority.issue(credential, peer(2, platform))
                    with authority.accept(peer(2, platform)) as server:
                        with ClientConnection(
                            ticket, peer(2, platform), peer(platform=platform)
                        ) as client:
                            client.receive(
                                server.receive(client.receive(server.receive(client.start())))
                            )
        ticket = self.authority.issue(self.credential, peer(2))
        for field, value in (
            ("platform", "linux"),
            ("user_id", "other"),
            ("os_session_id", "other"),
            ("process_id", 3),
            ("process_started", "started-2"),
            ("installation_digest", "b" * 64),
            ("instance_epoch", "next"),
        ):
            with self.subTest(field=field):
                changed = replace(peer(2), **{field: value})
                self.assert_code(
                    "AUTHENTICATION_FAILED",
                    lambda changed=changed: ClientConnection(ticket, changed, peer()),
                )
                self.assert_code(
                    "AUTHENTICATION_FAILED",
                    lambda field=field, value=value: ClientConnection(
                        ticket, peer(2), replace(peer(), **{field: value})
                    ),
                )

                def open_wrong(changed=changed):
                    server = self.authority.accept(changed)
                    client = ClientConnection(ticket, peer(2), peer())
                    server.receive(client.start())

                self.assert_code("AUTHENTICATION_FAILED", open_wrong)

    def test_challenge_and_ready_tampering_and_role_reflection(self):
        for stage in ("server-proof", "client-proof", "reflection", "ready-proof", "ready-session"):
            with self.subTest(stage=stage):
                ticket = self.authority.issue(self.credential, peer(2))
                with (
                    self.authority.accept(peer(2)) as server,
                    ClientConnection(ticket, peer(2), peer()) as client,
                ):
                    challenge = server.receive(client.start())
                    if stage == "server-proof":
                        challenge["proof"] = "0" * 64
                        self.assert_code(
                            "AUTHENTICATION_FAILED",
                            lambda challenge=challenge: client.receive(challenge),
                        )
                        continue
                    authenticate = client.receive(challenge)
                    if stage in ("client-proof", "reflection"):
                        authenticate["proof"] = (
                            "0" * 64 if stage == "client-proof" else challenge["proof"]
                        )
                        self.assert_code(
                            "AUTHENTICATION_FAILED",
                            lambda authenticate=authenticate: server.receive(authenticate),
                        )
                        self.assertTrue(server.closed)
                        continue
                    ready = server.receive(authenticate)
                    ready["proof" if stage == "ready-proof" else "session"] = "0" * 64
                    self.assert_code(
                        "AUTHENTICATION_FAILED", lambda ready=ready: client.receive(ready)
                    )

    def test_unknown_authentication_fields_and_out_of_order_packets_close(self):
        for stage in ("hello", "challenge", "authenticate", "ready"):
            with self.subTest(stage=stage):
                ticket = self.authority.issue(self.credential, peer(2))
                with (
                    self.authority.accept(peer(2)) as server,
                    ClientConnection(ticket, peer(2), peer()) as client,
                ):
                    value = client.start()
                    for current, target in (
                        ("hello", server),
                        ("challenge", client),
                        ("authenticate", server),
                        ("ready", client),
                    ):
                        if current == stage:
                            value["local"] = True
                            self.assert_code(
                                "INVALID_ARGUMENT",
                                lambda target=target, value=value: target.receive(value),
                            )
                            self.assertTrue(target.closed)
                            break
                        value = target.receive(value)
        with self.authority.accept(peer(2)) as server:
            self.assert_code("INVALID_ARGUMENT", lambda: server.receive({"kind": "request"}))
            self.assertTrue(server.closed)

    def test_one_ticket_is_consumed_once_under_concurrent_authentication(self):
        ticket = self.authority.issue(self.credential, peer(2))
        prepared = []
        for _ in range(2):
            client = ClientConnection(ticket, peer(2), peer())
            server = self.authority.accept(peer(2))
            self.addCleanup(server.close)
            prepared.append((server, client.receive(server.receive(client.start()))))
        barrier = threading.Barrier(2)

        def authenticate(item):
            barrier.wait(timeout=5)
            try:
                return item[0].receive(item[1])["kind"]
            except DeskWeaveError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(authenticate, prepared))
        self.assertCountEqual(results, ["connection.ready", "AUTHENTICATION_FAILED"])
        self.assert_code("AUTHENTICATION_FAILED", lambda: self.connect(ticket=ticket))

    def test_old_response_fails_on_new_nonce_or_ticket(self):
        ticket = self.authority.issue(self.credential, peer(2))
        with (
            self.authority.accept(peer(2)) as old,
            ClientConnection(ticket, peer(2), peer()) as client,
        ):
            replay = client.receive(old.receive(client.start()))
        for value in (ticket, self.authority.issue(self.credential, peer(2))):
            with (
                self.authority.accept(peer(2)) as server,
                ClientConnection(value, peer(2), peer()) as client,
            ):
                server.receive(client.start())
                self.assert_code("AUTHENTICATION_FAILED", lambda: server.receive(replay))

    def test_ticket_binding_includes_workspace_and_authorization(self):
        ticket = self.authority.issue(self.credential, peer(2))
        for field, value in (
            ("workspace", "other"),
            ("client_id", "other"),
            ("authorization_version", 2),
        ):
            with self.subTest(field=field):
                changed = replace(ticket, **{field: value})
                with (
                    self.authority.accept(peer(2)) as server,
                    ClientConnection(changed, peer(2), peer()) as client,
                ):
                    self.assert_code(
                        "AUTHENTICATION_FAILED",
                        lambda: client.receive(server.receive(client.start())),
                    )

    def test_deadlines_use_monotonic_time_and_release_capacity(self):
        with ConnectionAuthority(
            self.engine, peer(), max_clients=1, max_tickets=1, max_connections=1
        ) as authority:
            credential = authority.authorize(ConnectionPolicy(set()), lifetime=90)
            ticket = authority.issue(credential, peer(2))
            server = authority.accept(peer(2))
            self.assert_code(
                "LIMIT_EXCEEDED", lambda: authority.authorize(ConnectionPolicy(set()), lifetime=1)
            )
            self.assert_code("LIMIT_EXCEEDED", lambda: authority.issue(credential, peer(2)))
            self.assert_code("LIMIT_EXCEEDED", lambda: authority.accept(peer(2)))
            self.now[0] += 10000000
            with ClientConnection(ticket, peer(2), peer()) as client:
                authenticate = client.receive(server.receive(client.start()))
                self.elapsed[0] = 10
                self.assert_code(
                    "AUTHENTICATION_FAILED",
                    lambda authenticate=authenticate: server.receive(authenticate),
                )
            authority.sweep()
            self.assertTrue(server.closed)
            self.elapsed[0] = 60
            fresh = authority.issue(credential, peer(2))
            self.assertNotEqual(ticket.ticket_id, fresh.ticket_id)
            self.elapsed[0] = 90
            authority.sweep()
            authority.authorize(ConnectionPolicy(set()), lifetime=30)

    def test_ticket_expiry_before_hello_and_authorization_expiry_after_ready(self):
        ticket = self.authority.issue(self.credential, peer(2))
        self.elapsed[0] = 60
        self.assert_code("AUTHENTICATION_FAILED", lambda: self.connect(ticket=ticket))
        pair = self.connect()
        self.elapsed[0] = 300
        self.assert_code(
            "ACCESS_DENIED", lambda: self.call(pair, "workspace.catalog", {"kind": "workflow"})
        )
        self.assertTrue(pair[1].closed)

    def test_permissions_objects_and_wire_fields_are_enforced(self):
        credential = self.authority.authorize(
            ConnectionPolicy({"workflow.read"}, {"hello"}), lifetime=60
        )
        pair = self.connect(credential=credential)
        for method, params in (
            ("workflow.save", self.params()),
            ("expression.preview", {"value": {"kind": "literal", "value": 1}}),
            ("workflow.read", {"workflowId": "other", "revision": "r"}),
        ):
            result = self.call(pair, method, params)
            self.assertEqual(result["error"]["code"], "ACCESS_DENIED")
        for field, value in (
            ("local", True),
            ("permissions", ["workflow.write"]),
            ("clientId", "studio"),
        ):
            request = pair[0].request("workspace.catalog", {"kind": "workflow"})
            request[field] = value
            self.assertEqual(pair[1].receive(request)["error"]["code"], "INVALID_ARGUMENT")
        for field in ("session", "workspace"):
            request = pair[0].request("workspace.catalog", {"kind": "workflow"})
            request[field] = "other"
            self.assertEqual(pair[1].receive(request)["error"]["code"], "ACCESS_DENIED")
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM revision").fetchone()[0], 0)

    def test_authorization_revision_closes_old_sessions_and_tickets(self):
        pair = self.connect()
        ticket = self.authority.issue(self.credential, peer(2))
        version = self.authority.revise(
            self.credential, ConnectionPolicy({"workflow.read"}), expected_version=1, lifetime=60
        )
        self.assertEqual(version, 2)
        self.assertTrue(pair[1].closed)
        self.assert_code("AUTHENTICATION_FAILED", lambda: self.connect(ticket=ticket))
        restored = self.connect()
        self.assertEqual(restored[1].session.authorization_version, 2)
        self.assertEqual(restored[0].client_id, self.credential.client_id)
        self.assertEqual(
            self.call(restored, "workflow.save", self.params())["error"]["code"], "ACCESS_DENIED"
        )
        self.assert_code(
            "AUTHORIZATION_CONFLICT",
            lambda: self.authority.revise(
                self.credential, ConnectionPolicy(set()), expected_version=1, lifetime=60
            ),
        )
        self.authority.revoke(self.credential)
        self.authority.revoke(self.credential)
        self.assertTrue(restored[1].closed)

    def test_invalid_recovery_secret_and_expired_revocation(self):
        forged = ClientCredential(self.credential.client_id, b"x" * 32)
        self.assert_code("ACCESS_DENIED", lambda: self.authority.issue(forged, peer(2)))
        self.assert_code("ACCESS_DENIED", lambda: self.authority.revoke(forged))
        pair = self.connect()
        self.elapsed[0] = 300
        self.authority.revoke(self.credential)
        self.assertTrue(pair[1].closed)

    def test_negotiated_limits_return_complete_error_and_reject_large_request(self):
        self.save()
        pair = self.connect(max_frame_bytes=1024)
        saved = self.success("workspace.catalog", {"kind": "workflow"})["items"][0]
        result = self.call(
            pair, "workflow.read", {"workflowId": "hello", "revision": saved["version"]}
        )
        self.assertEqual(result["error"]["code"], "LIMIT_EXCEEDED")
        request = pair[0].request("workspace.catalog", {"kind": "workflow"})
        request["params"]["nameContains"] = "x" * 1024
        self.assert_code("LIMIT_EXCEEDED", lambda: pair[1].receive(request))
        self.assertTrue(pair[1].closed)

    def test_negotiated_query_limit_rolls_back_snapshots(self):
        for index in range(4):
            self.save(workflow(identity=f"flow-{index}", name="界" * 100))
        pair = self.connect(max_frame_bytes=1024)
        for _ in range(10):
            response = self.call(pair, "workspace.catalog", {"kind": "workflow"})
            self.assertEqual(response["error"]["code"], "LIMIT_EXCEEDED")
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM query_snapshot WHERE owner=?", (pair[0].session,)
                ).fetchone()[0],
                0,
            )
        page = self.call(pair, "workspace.catalog", {"kind": "workflow", "pageSize": 1})
        self.assertTrue(page["ok"], page)
        self.assertIsNotNone(page["result"]["nextCursor"])

    def test_client_checks_method_result_and_original_session(self):
        for field in ("result", "session", "workspace"):
            with self.subTest(field=field):
                client, _ = self.connect()
                request = client.request("workflow.read", {"workflowId": "hello", "revision": "r"})
                response = {
                    "protocolVersion": "1.0",
                    "kind": "response",
                    "replyTo": request["id"],
                    "ok": True,
                    "result": 42,
                }
                if field != "result":
                    request[field] = "other"
                self.assert_code(
                    "INVALID_ARGUMENT",
                    lambda client=client, response=response, request=request: client.response(
                        response, request=request
                    ),
                )
                self.assertTrue(client.closed)

    def test_response_correlation_and_handshake_snapshots(self):
        pair = self.connect()
        request = pair[0].request("workspace.catalog", {"kind": "workflow"})
        response = pair[1].receive(request)
        self.assert_code(
            "INVALID_ARGUMENT",
            lambda: pair[0].response(response, request={**request, "id": "other"}),
        )
        ticket = self.authority.issue(self.credential, peer(2))
        with (
            self.authority.accept(peer(2)) as server,
            ClientConnection(ticket, peer(2), peer()) as client,
        ):
            hello = client.start()
            altered = copy.deepcopy(hello)
            altered["nonce"] = "0" * 64
            self.assert_code(
                "AUTHENTICATION_FAILED", lambda: client.receive(server.receive(altered))
            )

    def test_disconnect_releases_content_pages_and_diagnostics(self):
        pair = self.connect()
        self.call(pair, "workflow.save", self.params())
        self.call(pair, "workspace.catalog", {"kind": "workflow", "pageSize": 1})
        definition = workflow()
        definition["entry"] = "missing"
        self.call(pair, "workflow.validate", {"definition": definition})
        raw = dumps({"definition": workflow(), "dependencies": {}})
        upload = self.call(
            pair,
            "content.begin",
            {
                "purpose": "workflowModel",
                "mediaType": "application/json",
                "length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        self.assertTrue(upload["ok"], upload)
        owner = pair[0].session
        self.assertTrue(self.engine.contents.uploads)
        self.assertTrue(self.engine._collections)
        pair[1].close()
        self.assertNotIn(owner, self.engine._sessions)
        self.assertEqual(self.engine.contents.uploads, {})
        self.assertEqual(self.engine._collections, {})
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM query_snapshot WHERE owner=?", (owner,)
                ).fetchone()[0],
                0,
            )

    def test_revocation_during_save_rolls_back_before_commit(self):
        for phase in ("object", "receipt"):
            with self.subTest(phase=phase):
                credential = self.authority.authorize(
                    ConnectionPolicy({"workflow.read", "workflow.write"}), lifetime=300
                )
                pair = self.connect(credential=credential)
                entered, invalidated, resume = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                original_put = self.workspace._put_object
                original_close = pair[1].close

                def wait(entered=entered, resume=resume):
                    entered.set()
                    if not resume.wait(5):
                        raise RuntimeError("save was not resumed")

                def put(value, wait=wait, original_put=original_put):
                    wait()
                    return original_put(value)

                def trace(statement, wait=wait):
                    if statement.startswith("INSERT INTO command_receipt"):
                        wait()

                def close(invalidated=invalidated, original_close=original_close):
                    invalidated.set()
                    original_close()

                self.workspace._connection.set_trace_callback(trace if phase == "receipt" else None)
                with (
                    patch.object(
                        self.workspace,
                        "_put_object",
                        side_effect=put if phase == "object" else original_put,
                    ),
                    patch.object(pair[1], "close", side_effect=close),
                ):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        operation = pool.submit(self.call, pair, "workflow.save", self.params())
                        try:
                            self.assertTrue(entered.wait(5))
                            revoked = pool.submit(self.authority.revoke, credential)
                            self.assertTrue(invalidated.wait(5))
                        finally:
                            resume.set()
                        self.assert_code(
                            "ACCESS_DENIED", lambda operation=operation: operation.result(timeout=5)
                        )
                        revoked.result(timeout=5)
                self.workspace._connection.set_trace_callback(None)
                with self.workspace.transaction() as connection:
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM revision").fetchone()[0], 0
                    )
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM command_receipt").fetchone()[0], 0
                    )

    def test_sql_progress_interrupt_preserves_authorization_error(self):
        pair = self.connect()
        calls = 0
        original_check = self.authority.check
        querying = False

        def check(*args):
            nonlocal calls
            if querying:
                calls += 1
                if calls == 5:
                    self.elapsed[0] = 300
            original_check(*args)

        def handler(*_args):
            nonlocal querying
            querying = True
            self.workspace._connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL "
                "SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n"
            ).fetchone()

        original = self.engine._methods["workspace.catalog"]
        self.engine._methods["workspace.catalog"] = replace(original, handler=handler)
        with patch.object(self.authority, "check", side_effect=check):
            self.assert_code(
                "ACCESS_DENIED", lambda: self.call(pair, "workspace.catalog", {"kind": "workflow"})
            )
        self.assertTrue(pair[1].closed)
        self.assertFalse(self.workspace._connection.in_transaction)
        self.engine._methods["workspace.catalog"] = original
        self.assertTrue(self.request("workspace.catalog", {"kind": "workflow"})["ok"])

    def test_closing_connection_keeps_capacity_until_cleanup_finishes(self):
        pair = self.connect()
        self.authority.max_connections = 1
        invalidated = threading.Event()
        retrying = threading.Event()
        original = pair[1].close
        attempts = 0
        lock = threading.Lock()

        def close():
            nonlocal attempts
            with lock:
                attempts += 1
                (invalidated if attempts == 1 else retrying).set()
            original()

        with patch.object(pair[1], "close", side_effect=close):
            with ThreadPoolExecutor(max_workers=2) as pool:
                with pair[1]._lock:
                    revoked = pool.submit(self.authority.revoke, self.credential)
                    self.assertTrue(invalidated.wait(5))
                    with self.authority._lock:
                        self.assertIn(pair[1], self.authority._closing)
                        self.assertEqual(len(self.authority._connections), 0)
                    accepted = pool.submit(self.authority.accept, peer(2))
                    self.assertTrue(retrying.wait(5))
                    self.assertFalse(accepted.done())
                revoked.result(timeout=5)
                new = accepted.result(timeout=5)
        self.assertTrue(pair[1].closed)
        self.assertEqual(self.authority._closing, set())
        new.close()

    def test_all_authority_shutdown_paths_register_cleanup_before_close(self):
        for mode in ("revise", "sweep", "close"):
            with self.subTest(mode=mode):
                self.elapsed[0] = 0
                with ConnectionAuthority(self.engine, peer()) as authority:
                    credential = authority.authorize(ConnectionPolicy(set()), lifetime=1)
                    ticket = authority.issue(credential, peer(2))
                    server = authority.accept(peer(2))
                    with ClientConnection(ticket, peer(2), peer()) as client:
                        client.receive(
                            server.receive(client.receive(server.receive(client.start())))
                        )
                    original = server.close

                    def close(authority=authority, server=server, original=original):
                        self.assertIn(server, authority._closing)
                        original()

                    with patch.object(server, "close", side_effect=close):
                        if mode == "revise":
                            authority.revise(
                                credential, ConnectionPolicy(set()), expected_version=1, lifetime=1
                            )
                        elif mode == "sweep":
                            self.elapsed[0] = 1
                            authority.sweep()
                        else:
                            authority.close()
                    self.assertEqual(authority._closing, set())

    def test_cancelled_stream_close_cleans_other_resources_and_retries(self):
        pair = self.connect()
        for _ in range(2):
            result = self.call(
                pair,
                "content.begin",
                {
                    "purpose": "workflowModel",
                    "mediaType": "application/json",
                    "length": 2,
                    "sha256": hashlib.sha256(b"{}").hexdigest(),
                },
            )
            self.assertTrue(result["ok"], result)
        streams = [item.stream for item in self.engine.contents.uploads.values()]
        item = next(iter(self.engine.contents.uploads.values()))
        original = item.stream

        class InterruptedStream:
            def __init__(self):
                self.attempts = 0

            @property
            def closed(self):
                return original.closed

            def close(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise KeyboardInterrupt("interrupted close")
                original.close()

        item.stream = InterruptedStream()
        with self.assertRaises(KeyboardInterrupt):
            pair[1].close()
        self.assertTrue(pair[1].closed)
        self.assertEqual(self.engine.contents.uploads, {})
        self.assertEqual(len(self.engine.contents._closing), 1)
        self.assertEqual(sum(stream.closed for stream in streams), 1)
        self.assertIn(pair[1], self.authority._closing)
        self.authority.sweep()
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(self.engine.contents._closing, {})
        self.assertEqual(self.engine._closing_sessions, {})
        self.assertEqual(self.authority._closing, set())
        pair[1].close()

    def test_cancellation_closes_session_and_preserves_cleanup_error(self):
        pair = self.connect()
        session = pair[0].session
        original = self.engine.close_session

        def failing(identity):
            original(identity)
            raise OSError("cleanup failed")

        with (
            patch.object(self.engine, "dispatch", side_effect=KeyboardInterrupt("cancel")),
            patch.object(self.engine, "close_session", side_effect=failing),
        ):
            with self.assertRaises(BaseExceptionGroup) as caught:
                self.call(pair, "workspace.catalog", {"kind": "workflow"})
        self.assertIsInstance(caught.exception.exceptions[0], KeyboardInterrupt)
        self.assertIsInstance(caught.exception.exceptions[1], OSError)
        self.assertTrue(pair[1].closed)
        self.assertNotIn(session, self.engine._sessions)

    def test_profiles_and_contract_digest_are_deterministic(self):
        current = ConnectionProfile.current(peer())
        compatible = replace(
            current, protocol_version="1.3", capabilities={"contentRefsV1", "future"}
        )
        self.assertEqual(negotiate(current, compatible)["protocolVersion"], "1.0")
        self.assertEqual(negotiate(current, compatible)["capabilities"], ["contentRefsV1"])
        self.assert_code(
            "PROTOCOL_UNSUPPORTED",
            lambda: negotiate(current, replace(current, protocol_version="2.0")),
        )
        self.assert_code(
            "SCHEMA_UNSUPPORTED",
            lambda: negotiate(current, replace(current, schema_digest="b" * 64)),
        )
        root = Path(__file__).resolve().parents[2] / "contracts"
        documents = {}
        for path in root.glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            key = (
                "schema:" + value["$id"]
                if path.name.endswith(".schema.json")
                else "catalog:" + path.name
            )
            documents[key] = value
        self.assertEqual(
            contracts.contract_digest(),
            hashlib.sha256(dumps(documents, max_bytes=32 * 1024 * 1024)).hexdigest(),
        )
        for value in (True, 0, 9007199254740992):
            with self.assertRaises(ValueError):
                self.engine.open_session("client", set(), authorization_version=value)
