import base64
import hashlib
import io
import json
import sqlite3
from unittest.mock import patch

from PIL import Image

from deskweave.adapters.assets import Assets
from deskweave.adapters.blobs import Blobs
from deskweave.adapters.workspace import Workspace
from deskweave.domain.assets import validate_trajectory
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps
from tests.engine.test_service import ROOT, EngineCase, workflow


def trajectory(x=1):
    return {
        "formatVersion": "1.0",
        "coordinateMode": "physical",
        "events": [
            {"kind": "pointer.button", "atUs": 0, "button": "left", "state": "down"},
            {"kind": "pointer.move", "atUs": 1, "x": x, "y": 2},
            {"kind": "pointer.move", "atUs": 1, "x": x + 1, "y": 3},
            {"kind": "pointer.button", "atUs": 2, "button": "left", "state": "up"},
        ],
    }


class AssetsTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.assets = Assets(self.workspace)
        self.editor = self.engine.open_session(
            "assets",
            {"workflow.read", "workflow.write", "asset.read"},
            local=True,
            capabilities={"contentRefsV1"},
        )

    def tearDown(self):
        self.engine.close()
        super().tearDown()

    def asset(self, identity="line", name="线条", x=1, base=None):
        return self.assets.put(
            identity,
            name,
            "trajectory",
            io.BytesIO(dumps(trajectory(x))),
            expected_version=base,
            duration_us=4,
        )

    @staticmethod
    def binding(asset):
        return {key: asset[key] for key in ("kind", "assetId", "assetVersion", "sha256")}

    def params(self, identity, dependencies, *, base=None):
        definition = workflow(identity)
        definition["resources"] = {
            key: {"kind": value["kind"]} for key, value in dependencies.items()
        }
        return {
            "workflowId": identity,
            "draftVersion": 1,
            "baseRevision": base,
            "definition": definition,
            "dependencies": dependencies,
        }

    def bound(self, identity, dependencies, **kwargs):
        saved = self.success("workflow.save", self.params(identity, dependencies), **kwargs)
        return {"kind": "workflow", "workflowId": identity, "revision": saved["revision"]}

    def test_versions_preserve_raw_events_and_reopen(self):
        first = self.asset()
        second = self.asset(x=20, base=first["assetVersion"])
        with self.assertRaisesRegex(DeskWeaveError, "资产已更新"):
            self.asset(x=30, base=first["assetVersion"])
        self.engine.close()
        self.workspace = Workspace(self.directory)
        self.engine = self.make_engine()
        self.assets = Assets(self.workspace)
        for asset, x in [(first, 1), (second, 20)]:
            row, data = self.assets.read_range(asset["assetVersion"], 0, 10000)
            self.assertEqual(json.loads(data), trajectory(x))
            self.assertEqual(row["content_hash"], hashlib.sha256(data).hexdigest())
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM asset_version").fetchone()[0], 2
            )

    def test_names_use_nfc_without_case_folding(self):
        self.asset("a", "Cafe\u0301")
        with self.assertRaises(DeskWeaveError) as caught:
            self.asset("b", "Café")
        self.assertEqual(caught.exception.code, "NAME_CONFLICT")
        self.asset("b", "CAFÉ")
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM asset_version").fetchone()[0], 2
            )

    def test_real_image_decoding_and_corrupt_pixels(self):
        stream = io.BytesIO()
        Image.new("RGB", (32, 16), "red").save(stream, format="PNG")
        data = stream.getvalue()
        item = self.assets.put("image", "图像", "image", io.BytesIO(data))
        self.assertEqual(item["mediaType"], "image/png")
        self.assertEqual(item["metadata"], {"width": 32, "height": 16, "frames": 1})
        for raw in [b"not-image", data[:50], data[:-30]]:
            with self.assertRaises(DeskWeaveError):
                self.assets.put("bad", "损坏", "image", io.BytesIO(raw))
        with patch("deskweave.adapters.assets.MAX_IMAGE_PIXELS", 100):
            with self.assertRaises(DeskWeaveError) as caught:
                self.assets.put("large", "大图", "image", io.BytesIO(data))
            self.assertEqual(caught.exception.code, "LIMIT_EXCEEDED")

    def test_trajectory_validation_preserves_equal_timestamps_and_pairing(self):
        validate_trajectory(trajectory(), duration_us=4)
        variants = []
        value = trajectory()
        value["events"][1]["atUs"] = 3
        variants.append(value)
        value = trajectory()
        value["events"].pop()
        variants.append(value)
        value = trajectory()
        value["events"][0]["state"] = "up"
        variants.append(value)
        value = trajectory()
        value["coordinateMode"] = "relative"
        variants.append(value)
        for value in variants:
            with self.assertRaises(DeskWeaveError):
                validate_trajectory(value, duration_us=4)
        with self.assertRaises(DeskWeaveError):
            validate_trajectory(trajectory(), duration_us=2)

    def test_trajectory_contract_fixtures(self):
        cases = json.loads((ROOT / "examples/cases.json").read_text(encoding="utf-8"))
        for case in cases:
            if case["schema"] != "trajectory.schema.json":
                continue
            with self.subTest(file=case["file"]):
                value = json.loads((ROOT / "examples" / case["file"]).read_text(encoding="utf-8"))
                if case["expected"] == "pass":
                    validate_trajectory(value)
                else:
                    with self.assertRaises(DeskWeaveError):
                        validate_trajectory(value)

    def test_blob_streaming_limits_corruption_and_interruption(self):
        blobs = Blobs(self.workspace)
        data = b"abc" * 100000

        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 71))

        identity, length = blobs.put(ShortReads(data))
        with blobs.open(identity, length) as stream:
            self.assertEqual(stream.read(), data)
        with self.assertRaises(DeskWeaveError):
            blobs.put(io.BytesIO(data), maximum=10)
        (self.directory / "objects/sha256" / identity).write_bytes(b"x" * length)
        with self.assertRaises(DeskWeaveError) as caught:
            blobs.put(io.BytesIO(data))
        self.assertEqual(caught.exception.code, "STORAGE_CORRUPT")
        with patch.object(
            self.workspace,
            "check_deadline",
            side_effect=DeskWeaveError("DEADLINE_EXCEEDED", "请求处理超时。"),
        ):
            with self.assertRaises(DeskWeaveError):
                blobs.put(io.BytesIO(data))
        self.assertEqual(list((self.directory / "objects/sha256").glob(".writing-*")), [])

    def test_bound_assets_and_children_keep_versions_after_updates(self):
        first = self.asset()
        child = self.bound("child", {"line": self.binding(first)})
        parent = self.bound("parent", {"child": child})
        self.asset(x=50, base=first["assetVersion"])
        changed = self.params("child", {}, base=child["revision"])
        self.success("workflow.save", changed)
        result = self.success(
            "workflow.read", {key: parent[key] for key in ("workflowId", "revision")}
        )
        self.assertEqual(result["dependencies"], {"child": child})
        result = self.success(
            "workflow.read", {key: child[key] for key in ("workflowId", "revision")}
        )
        self.assertEqual(result["dependencies"]["line"], self.binding(first))
        with self.workspace.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM revision_asset_ref").fetchone()[1],
                first["assetVersion"],
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM asset_version WHERE id=?", (first["assetVersion"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM revision WHERE id=?", (child["revision"],))

    def test_embedded_child_checked_and_canonicalized(self):
        child = self.bound("child", {"line": self.binding(self.asset())})
        original = self.success(
            "workflow.read", {key: child[key] for key in ("workflowId", "revision")}
        )
        embedded = child | {key: original[key] for key in ("definition", "dependencies")}
        parent = self.bound("parent", {"child": embedded})
        result = self.success(
            "workflow.read", {key: parent[key] for key in ("workflowId", "revision")}
        )
        self.assertEqual(result["dependencies"], {"child": child})
        embedded["definition"]["name"] = "伪造内容"
        response = self.request("workflow.save", self.params("forged", {"child": embedded}))
        self.assertEqual(response["error"]["code"], "DEPENDENCY_MISMATCH")

    def test_dependency_mismatches_do_not_commit(self):
        binding = self.binding(self.asset())
        for field, value in [
            ("assetId", "other"),
            ("assetVersion", "absent"),
            ("sha256", "0" * 64),
            ("kind", "image"),
        ]:
            with self.subTest(field=field):
                invalid = binding | {field: value}
                response = self.request("workflow.save", self.params("bad", {"line": invalid}))
                self.assertFalse(response["ok"])
        params = self.params("bad", {"line": binding})
        params["dependencies"] = {}
        self.assertEqual(
            self.request("workflow.save", params)["error"]["code"], "DEPENDENCY_MISMATCH"
        )
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM revision").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM command_receipt").fetchone()[0], 0
            )

    def test_transitive_authorization_and_corruption(self):
        asset = self.asset()
        child = self.bound("child", {"line": self.binding(asset)})
        restricted = self.engine.open_session(
            "restricted", {"workflow.read", "workflow.write", "asset.read"}, workflow_ids={"parent"}
        )
        params = self.params("parent", {"child": child})
        self.assertEqual(
            self.request("workflow.save", params, session=restricted)["error"]["code"],
            "ACCESS_DENIED",
        )
        no_assets = self.engine.open_session("no-assets", {"workflow.read", "workflow.write"})
        self.assertEqual(
            self.request("workflow.save", params, session=no_assets)["error"]["code"],
            "ACCESS_DENIED",
        )
        parent = self.bound("parent", {"child": child})
        request = {key: parent[key] for key in ("workflowId", "revision")}
        self.assertEqual(
            self.request("workflow.read", request, session=no_assets)["error"]["code"],
            "ACCESS_DENIED",
        )
        (self.directory / "objects/sha256" / asset["sha256"]).unlink()
        self.assertEqual(self.request("workflow.read", request)["error"]["code"], "STORAGE_CORRUPT")

    def test_shared_dependency_visited_once(self):
        leaf = self.bound("leaf", {"line": self.binding(self.asset())})
        left = self.bound("left", {"leaf": leaf})
        right = self.bound("right", {"leaf": leaf})
        with patch.object(self.workspace, "read", wraps=self.workspace.read) as read:
            self.bound("root", {"left": left, "right": right})
        self.assertEqual(read.call_count, 3)
        with patch("deskweave.adapters.dependencies.MAX_VERSIONS", 2):
            result = self.request("workflow.save", self.params("limited", {"left": left}))
        self.assertEqual(result["error"]["code"], "LIMIT_EXCEEDED")

    def test_cycle_in_stored_dependencies_is_rejected(self):
        child = self.bound("child", {})
        model = self.params("child", {"self": child})
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE revision SET definition_hash=?,dependencies_hash=? WHERE id=?",
                (
                    self.workspace._put_object(model["definition"]),
                    self.workspace._put_object(model["dependencies"]),
                    child["revision"],
                ),
            )
        result = self.request("workflow.save", self.params("parent", {"child": child}))
        self.assertEqual(result["error"]["code"], "DEPENDENCY_CYCLE")

    def test_reference_failure_rolls_back_save_and_receipt(self):
        binding = self.binding(self.asset())
        with self.workspace.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_ref BEFORE INSERT ON revision_asset_ref "
                "BEGIN SELECT RAISE(ABORT, 'failed reference'); END"
            )
        result = self.request(
            "workflow.save", self.params("parent", {"line": binding}), request_id="fail"
        )
        self.assertEqual(result["error"]["code"], "STORAGE_UNAVAILABLE")
        with self.workspace.transaction() as connection:
            for table in ["workflow", "revision", "revision_asset_ref", "command_receipt"]:
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
            connection.execute("DROP TRIGGER fail_ref")
        first = self.success(
            "workflow.save", self.params("parent", {"line": binding}), request_id="fail"
        )
        self.assertEqual(
            self.success(
                "workflow.save", self.params("parent", {"line": binding}), request_id="fail"
            ),
            first,
        )

    def test_asset_slice_handles_permissions_and_end_of_file(self):
        asset = self.asset()
        descriptor = self.success(
            "asset.read", {"assetVersion": asset["assetVersion"], "offset": 4, "length": 30}
        )
        block = self.success(
            "resource.read", {"resourceId": descriptor["resourceId"], "offset": 0, "length": 100}
        )
        self.assertEqual(base64.b64decode(block["chunk"]), dumps(trajectory())[4:34])
        self.assertEqual(
            descriptor["sha256"], hashlib.sha256(dumps(trajectory())[4:34]).hexdigest()
        )
        self.assertEqual(block["totalLength"], 30)
        other = self.engine.open_session("other", {"asset.read"}, capabilities={"contentRefsV1"})
        self.assertEqual(
            self.request(
                "resource.read",
                {"resourceId": descriptor["resourceId"], "offset": 0, "length": 1},
                session=other,
            )["error"]["code"],
            "RESOURCE_NOT_FOUND",
        )
        end = self.success(
            "asset.read",
            {"assetVersion": asset["assetVersion"], "offset": asset["length"], "length": 1},
        )
        self.assertEqual(end["length"], 0)
        self.assertEqual(
            self.request(
                "asset.read",
                {"assetVersion": asset["assetVersion"], "offset": asset["length"] + 1, "length": 1},
            )["error"]["code"],
            "INVALID_ARGUMENT",
        )
        self.now[0] += 61
        self.assertEqual(
            self.request(
                "resource.read", {"resourceId": descriptor["resourceId"], "offset": 0, "length": 1}
            )["error"]["code"],
            "RESOURCE_NOT_FOUND",
        )

    def test_asset_catalog_keeps_snapshot_across_updates(self):
        first = self.asset("a", "a")
        second = self.asset("b", "b")
        params = {"kind": "asset", "pageSize": 1, "order": "nameAsc"}
        page = self.success("workspace.catalog", params)
        self.assertEqual(page["items"][0]["id"], "a")
        self.asset("b", "0", base=second["assetVersion"])
        self.asset("a", "z", base=first["assetVersion"])
        next_page = self.success("workspace.catalog", params | {"cursor": page["nextCursor"]})
        self.assertEqual(
            next_page["items"], [{"id": "b", "name": "b", "version": second["assetVersion"]}]
        )
        self.assertIsNone(next_page["nextCursor"])
        for order in ["nameAsc", "nameDesc", "updatedDesc"]:
            result = self.success("workspace.catalog", {"kind": "asset", "order": order})
            self.assertEqual(len(result["items"]), 2)
        self.assertEqual(
            self.request("workspace.catalog", {"kind": "asset"}, session=self.remote)["error"][
                "code"
            ],
            "ACCESS_DENIED",
        )

    def test_range_checks_only_covering_blocks(self):
        value = {
            "formatVersion": "1.0",
            "coordinateMode": "physical",
            "events": [{"kind": "text", "atUs": i, "text": "x" * 200000} for i in range(4)],
        }
        raw = dumps(value)
        asset = self.assets.put("text", "文本", "trajectory", io.BytesIO(raw))
        with patch.object(Blobs, "open", side_effect=AssertionError("full scan")):
            _, data = self.assets.read_range(asset["assetVersion"], 190000, 200000)
            self.assertEqual(data, raw[190000:390000])
        path = self.directory / "objects/sha256" / asset["sha256"]
        with path.open("r+b") as stream:
            stream.write(b"!")
        _, data = self.assets.read_range(asset["assetVersion"], 400000, 10)
        self.assertEqual(data, raw[400000:400010])
        with self.assertRaises(DeskWeaveError) as caught:
            self.assets.read_range(asset["assetVersion"], 0, 10)
        self.assertEqual(caught.exception.code, "STORAGE_CORRUPT")
        with self.workspace.transaction() as connection:
            connection.execute(
                "DELETE FROM blob_chunk WHERE content_hash=? AND position=2", (asset["sha256"],)
            )
        with self.assertRaises(DeskWeaveError):
            self.assets.read_range(asset["assetVersion"], 400000, 10)

    def test_version_two_database_retains_saved_models(self):
        original = self.save()
        with self.workspace.transaction() as connection:
            for table in [
                "revision_asset_ref",
                "revision_workflow_ref",
                "asset_name_claim",
                "asset_version",
                "asset",
                "blob_chunk",
            ]:
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version=2")
        self.engine.close()
        self.workspace = Workspace(self.directory)
        self.engine = self.make_engine()
        self.assets = Assets(self.workspace)
        self.assertEqual(
            self.workspace.read("hello", original["revision"])["definition"], workflow()
        )
        self.asset()
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_asset_commit_deadline_rolls_back_registration(self):
        checkpoint = self.workspace.check_deadline

        def check():
            checkpoint()
            if (
                self.workspace._connection.in_transaction
                and self.workspace._connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
            ):
                raise DeskWeaveError("DEADLINE_EXCEEDED", "请求处理超时。")

        with patch.object(self.workspace, "check_deadline", side_effect=check):
            with self.assertRaises(DeskWeaveError):
                self.asset()
        with self.workspace.transaction() as connection:
            for table in ["asset", "asset_version", "asset_name_claim", "blob_chunk"]:
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
        self.asset()

    def test_normalized_name_limit_and_long_asset_identity(self):
        with self.assertRaises(DeskWeaveError) as caught:
            self.asset(name="\u0344" * 256)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        asset = self.asset("a" * 256)
        self.bound("long", {"line": self.binding(asset)})
        self.assertEqual(len(self.success("workspace.catalog", {"kind": "asset"})["items"]), 1)

    def test_validation_uses_private_snapshot(self):
        original = self.asset()
        path = self.directory / "objects/sha256" / original["sha256"]
        altered = dumps(trajectory(2))
        self.assertEqual(len(altered), original["length"])

        def change_published(value, **kwargs):
            result = validate_trajectory(value, **kwargs)
            path.write_bytes(altered)
            return result

        with patch("deskweave.adapters.assets.validate_trajectory", side_effect=change_published):
            with self.assertRaises(DeskWeaveError) as caught:
                self.asset("another", "另一条线")
        self.assertEqual(caught.exception.code, "STORAGE_CORRUPT")
        with self.workspace.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_asset_range_accepts_json_integral_numbers(self):
        asset = self.asset()
        for offset, length in [(0.0, 1), (0, 1.0), (float(asset["length"]), 1.0)]:
            with self.subTest(offset=offset, length=length):
                descriptor = self.success(
                    "asset.read",
                    {
                        "assetVersion": asset["assetVersion"],
                        "offset": offset,
                        "length": length,
                    },
                )
                self.assertEqual(descriptor["length"], 0 if offset == asset["length"] else 1)

    def test_trajectory_integral_timestamp_keeps_original_bytes(self):
        value = trajectory()
        value["events"][-1]["atUs"] = 2.0
        raw = json.dumps(value).encode()
        asset = self.assets.put("float-time", "轨迹时间", "trajectory", io.BytesIO(raw))
        self.assertEqual(asset["metadata"]["durationUs"], 3)
        self.assertEqual(self.assets.read_range(asset["assetVersion"], 0, len(raw))[1], raw)

    def test_image_formats_use_selected_decoders(self):
        from deskweave.adapters.assets import IMAGE_FORMATS

        for format_ in IMAGE_FORMATS:
            with self.subTest(format=format_):
                stream = io.BytesIO()
                Image.new("RGB", (32, 32), "blue").save(stream, format=format_)
                result = self.assets.put(format_, format_, "image", io.BytesIO(stream.getvalue()))
                self.assertEqual(result["metadata"]["width"], 32)
        with self.assertRaises(DeskWeaveError):
            self.assets.put(
                "postscript",
                "PostScript",
                "image",
                io.BytesIO(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"),
            )
