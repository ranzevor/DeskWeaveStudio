from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from deskweave.json import dumps
from deskweave.transport.protocol import PeerIdentity

from ._common import Budget, Resource, rejected

if sys.platform == "win32":
    from . import _windows as platform
elif sys.platform == "darwin":
    from . import _macos as platform
elif sys.platform == "linux":
    from . import _linux as platform
else:
    raise ImportError("当前平台没有本机 IPC 后端。")


def current_executable() -> Path:
    return platform.current_executable()


def stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns if sys.platform != "win32" else 0,
    )


def digest_file(path, checkpoint=lambda: None):
    with path.open("rb") as stream:
        before = stamp(os.fstat(stream.fileno()))
        accumulator = hashlib.sha256()
        while True:
            checkpoint()
            chunk = stream.read(65536)
            if not chunk:
                break
            accumulator.update(chunk)
        digest = accumulator.hexdigest()
        after = stamp(os.fstat(stream.fileno()))
    if before != after or after != stamp(path.stat()):
        raise rejected()
    return digest, after


@dataclass(frozen=True)
class InstalledFile:
    path: Path
    sha256: str

    def __post_init__(self):
        object.__setattr__(self, "path", Path(self.path).resolve(strict=True))
        if not isinstance(self.sha256, str) or not re.fullmatch("[0-9a-f]{64}", self.sha256):
            raise ValueError("安装文件需要 SHA-256 摘要。")


@dataclass(frozen=True)
class Installation:
    executable: InstalledFile
    components: tuple[InstalledFile, ...]
    macos_requirement: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "components", tuple(self.components))
        if not isinstance(self.executable, InstalledFile) or not self.components:
            raise ValueError("安装策略需要运行映像和组件清单。")
        if len(self.components) > 10000 or any(
            type(item) is not InstalledFile for item in self.components
        ):
            raise ValueError("安装组件清单无效。")
        if len({item.path for item in self.components}) != len(self.components):
            raise ValueError("安装组件清单包含重复路径。")
        if sys.platform == "darwin" and (
            not isinstance(self.macos_requirement, str) or not self.macos_requirement.strip()
        ):
            raise ValueError("macOS 安装策略需要签名 requirement。")

    @property
    def digest(self):
        return self.calculate_digest()

    def calculate_digest(self, checkpoint=lambda: None):
        accumulator = hashlib.sha256()
        accumulator.update(b'{"components":[')
        for index, item in enumerate(sorted(self.components, key=lambda value: str(value.path))):
            checkpoint()
            if index:
                accumulator.update(b",")
            accumulator.update(dumps([str(item.path), item.sha256], checkpoint=checkpoint))
        accumulator.update(b'],"executable":')
        accumulator.update(
            dumps([str(self.executable.path), self.executable.sha256], checkpoint=checkpoint)
        )
        accumulator.update(b',"macosRequirement":')
        accumulator.update(dumps(self.macos_requirement, checkpoint=checkpoint))
        accumulator.update(b"}")
        return accumulator.hexdigest()


class ProcessBinding(Resource):
    def __init__(
        self,
        pid: int,
        installation: Installation,
        *,
        instance_epoch: str,
        timeout=30.0,
        cancel=None,
    ):
        if (
            type(pid) is not int
            or not 1 <= pid <= 0xFFFFFFFF
            or type(installation) is not Installation
        ):
            raise ValueError("受控进程登记需要进程号和安装策略。")
        budget = Budget(timeout, cancel)
        self.installation = installation
        self.process = platform.Process(pid)
        try:
            self._snapshot = self.process.initial
            user, session, started, executable = self._snapshot[:4]
            if not executable.samefile(installation.executable.path):
                raise rejected()
            self.identity = PeerIdentity(
                platform.Process.platform,
                user,
                session,
                pid,
                started,
                installation.calculate_digest(budget.check_time),
                instance_epoch,
            )
            self._stamps = self._verify_installation(budget.check_time)
            self._check_process(budget.check_time)
        except BaseException:
            self.process.close()
            raise

    def _verify_installation(self, checkpoint=lambda: None):
        expected = self.installation
        image = (
            self.process.image_path() if hasattr(self.process, "image_path") else self._snapshot[3]
        )
        if digest_file(image, checkpoint)[0] != expected.executable.sha256:
            raise rejected()
        values = {}
        for item in (expected.executable, *expected.components):
            digest, state = digest_file(item.path, checkpoint)
            if digest != item.sha256:
                raise rejected()
            values[item.path] = state
        return values

    def _check_process(self, checkpoint=lambda: None):
        checkpoint()
        if self.process.snapshot() != self._snapshot:
            raise rejected()

    def check(self, checkpoint=lambda: None):
        self._check_process(checkpoint)
        if self._verify_installation(checkpoint) != self._stamps:
            raise rejected()
        self._check_process(checkpoint)

    def verify(self, raw, checkpoint=lambda: None):
        self._check_process(checkpoint)
        pid, credential = raw.peer()
        if pid != self.identity.process_id:
            raise rejected()
        if sys.platform != "win32":
            platform.verify_peer(self.process, credential, self.installation.macos_requirement)
        self.check(checkpoint)
        return self.identity

    def close(self):
        self.process.close()
