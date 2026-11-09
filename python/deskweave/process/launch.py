from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from deskweave.errors import DeskWeaveError
from deskweave.transport.native import Installation, InstalledFile
from deskweave.transport.native.identity import digest_file

ENTRY = Path(__file__).with_name("_entry.py").resolve()


@dataclass(frozen=True)
class PythonLaunch:
    launcher: InstalledFile
    installation: Installation
    import_paths: tuple[Path, ...]

    def __post_init__(self):
        if type(self.launcher) is not InstalledFile or type(self.installation) is not Installation:
            raise TypeError("启动配置需要解释器和安装策略。")
        paths = tuple(Path(path).resolve(strict=True) for path in self.import_paths)
        if not 1 <= len(paths) <= 64 or any(not path.is_dir() for path in paths):
            raise ValueError("启动模块目录数量或类型无效。")
        object.__setattr__(self, "import_paths", paths)
        entries = {
            item.path: item.sha256
            for item in (
                self.installation.executable,
                *self.installation.components,
            )
        }
        if entries.get(self.launcher.path) != self.launcher.sha256 or ENTRY not in entries:
            raise ValueError("安装清单需要包含启动解释器和进程入口。")

    def verify(self, checkpoint):
        for item in (self.installation.executable, *self.installation.components):
            if digest_file(item.path, checkpoint)[0] != item.sha256:
                raise DeskWeaveError("AUTHENTICATION_FAILED", "启动文件与安装清单不一致。")

    def environment(self):
        names = ("SystemRoot", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
        return {name: os.environ[name] for name in names if name in os.environ}

    def command(self):
        return [str(self.launcher.path), "-I", "-S", "-X", "utf8", str(ENTRY)]
