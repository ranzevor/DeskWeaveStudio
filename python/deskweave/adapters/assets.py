from __future__ import annotations

import unicodedata
import uuid
from tempfile import TemporaryFile

from deskweave.adapters.blobs import MAX_BLOB, Blobs
from deskweave.adapters.workspace import utc_time
from deskweave.domain.assets import validate_trajectory
from deskweave.errors import DeskWeaveError
from deskweave.json import dumps, loads

MAX_IMAGE_PIXELS = 64_000_000
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "BMP", "GIF", "TIFF", "ICO")


class Assets:
    def __init__(self, workspace):
        self.workspace = workspace
        self.blobs = Blobs(workspace)

    def put(self, asset_id, name, kind, source, *, expected_version=None, duration_us=None):
        if (
            type(asset_id) is not str
            or not 1 <= len(asset_id) <= 256
            or type(name) is not str
            or kind not in {"image", "trajectory"}
        ):
            raise DeskWeaveError("INVALID_ARGUMENT", "资产身份、名称或类型无效。")
        name = unicodedata.normalize("NFC", name.strip())
        if not 1 <= len(name) <= 256:
            raise DeskWeaveError("INVALID_ARGUMENT", "资产名称长度应为 1～256。")
        with self.workspace.budget(None):
            with TemporaryFile(mode="w+b") as stream:
                identity, length = self.blobs.copy(source, stream)
                stream.seek(0)
                if kind == "trajectory":
                    value = loads(
                        stream.read(), max_bytes=MAX_BLOB, checkpoint=self.workspace.check_deadline
                    )
                    metadata = validate_trajectory(
                        value, duration_us=duration_us, checkpoint=self.workspace.check_deadline
                    )
                    media = "application/json"
                else:
                    media, metadata = self._image(stream)
                checksums = self.blobs.checksums(stream)
                stream.seek(0)
                if self.blobs.put(stream) != (identity, length):
                    raise DeskWeaveError("STORAGE_CORRUPT", "资产暂存内容发生变化。")
            with self.workspace.transaction() as connection:
                old = connection.execute("SELECT * FROM asset WHERE id=?", (asset_id,)).fetchone()
                if (old["current_version"] if old else None) != expected_version:
                    raise DeskWeaveError("REVISION_CONFLICT", "资产已更新，请读取当前版本后保存。")
                if old is not None and old["kind"] != kind:
                    raise DeskWeaveError("INVALID_ARGUMENT", "资产的新版本不能改变类型。")
                claim = connection.execute(
                    "SELECT asset_id FROM asset_name_claim WHERE namespace=? AND name_nfc=?",
                    (kind, name),
                ).fetchone()
                if claim is not None and claim[0] != asset_id:
                    raise DeskWeaveError("NAME_CONFLICT", "资产名称已被使用。")
                for position, checksum in enumerate(checksums):
                    self.workspace.check_deadline()
                    existing = connection.execute(
                        "SELECT sha256 FROM blob_chunk WHERE content_hash=? AND position=?",
                        (identity, position),
                    ).fetchone()
                    if existing is not None and existing[0] != checksum:
                        raise DeskWeaveError("STORAGE_CORRUPT", "资产内容分块索引损坏。")
                    connection.execute(
                        "INSERT OR IGNORE INTO blob_chunk VALUES(?,?,?)",
                        (identity, position, checksum),
                    )
                version = uuid.uuid4().hex
                created = utc_time()
                connection.execute(
                    "INSERT INTO asset(id,kind,name,current_version) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "current_version=excluded.current_version",
                    (asset_id, kind, name, version),
                )
                row = connection.execute(
                    "INSERT INTO asset_version(id,asset_id,content_hash,byte_length,media_type,"
                    "metadata,name,name_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        version,
                        asset_id,
                        identity,
                        length,
                        media,
                        dumps(metadata).decode(),
                        name,
                        name.casefold(),
                        created,
                    ),
                )
                if old:
                    connection.execute(
                        "UPDATE asset_version SET valid_until=? WHERE id=?",
                        (row.lastrowid, old["current_version"]),
                    )
                connection.execute("DELETE FROM asset_name_claim WHERE asset_id=?", (asset_id,))
                connection.execute(
                    "INSERT INTO asset_name_claim VALUES(?,?,?)", (kind, name, asset_id)
                )
            return {
                "assetId": asset_id,
                "assetVersion": version,
                "kind": kind,
                "sha256": identity,
                "length": length,
                "mediaType": media,
                "metadata": metadata,
            }

    def _image(self, stream):
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(stream, formats=IMAGE_FORMATS) as image:
                image.verify()
            stream.seek(0)
            with Image.open(stream, formats=IMAGE_FORMATS) as image:
                media = Image.MIME.get(image.format)
                width, height = image.size
                frames, pixels = getattr(image, "n_frames", 1), 0
                for index in range(frames):
                    self.workspace.check_deadline()
                    image.seek(index)
                    pixels += image.width * image.height
                    if pixels > MAX_IMAGE_PIXELS:
                        raise DeskWeaveError("LIMIT_EXCEEDED", "图像解码像素总量超过限制。")
                    image.load()
                self.workspace.check_deadline()
                if not media:
                    raise DeskWeaveError("INVALID_CONTENT", "图像格式缺少媒体类型。")
                return media, {"width": width, "height": height, "frames": frames}
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
        ) as error:
            raise DeskWeaveError("INVALID_CONTENT", "图像内容损坏或格式无效。") from error

    def get(self, version):
        with self.workspace.transaction() as connection:
            row = connection.execute(
                "SELECT v.*,a.kind FROM asset_version v JOIN asset a ON a.id=v.asset_id "
                "WHERE v.id=?",
                (version,),
            ).fetchone()
        if row is None:
            raise DeskWeaveError("RESOURCE_NOT_FOUND", "资产版本不存在。")
        return dict(row)

    def read_range(self, version, offset, length):
        with self.workspace.budget(None):
            row = self.get(version)
            data = self.blobs.read_range(row["content_hash"], row["byte_length"], offset, length)
            return row, data
