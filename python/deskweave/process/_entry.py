"""通过继承管道读取安装策略，再加载清单中的应用模块。"""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import struct
import sys
from pathlib import Path

MAX_FRAME = 1048576


def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("启动消息包含重复字段。")
        value[key] = item
    return value


def reject_constant(_value):
    raise ValueError("启动消息包含非法数值。")


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def entry(value):
    if type(value) is not dict or set(value) != {"path", "sha256"}:
        raise ValueError("安装文件字段无效。")
    if type(value["path"]) is not str or not os.path.isabs(value["path"]):
        raise ValueError("安装文件路径必须为绝对路径。")
    digest = value["sha256"]
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("安装文件摘要无效。")
    return {"path": str(Path(value["path"]).resolve(strict=True)), "sha256": digest}


def read(stream):
    def exact(size):
        data = bytearray()
        while len(data) < size:
            part = stream.read(size - len(data))
            if not part:
                raise EOFError("启动输入不完整。")
            data.extend(part)
        return data

    size = struct.unpack("<I", exact(4))[0]
    if not 1 <= size <= MAX_FRAME:
        raise ValueError("启动消息长度无效。")
    return json.loads(exact(size), object_pairs_hook=pairs, parse_constant=reject_constant)


def manifest(stream, header, budget):
    if type(header) is not dict or set(header) != {
        "executable",
        "componentCount",
        "macosRequirement",
    }:
        raise ValueError("安装清单头无效。")
    count = header["componentCount"]
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError("安装组件数量无效。")
    header["executable"] = entry(header["executable"])
    budget[0] -= encoded_size(header)
    if budget[0] < 0:
        raise ValueError("启动安装元数据超过容量。")
    entries = []
    while len(entries) < count:
        batch = read(stream)
        if (
            type(batch) is not list
            or not 1 <= len(batch) <= 128
            or len(entries) + len(batch) > count
        ):
            raise ValueError("安装分块数量无效。")
        for value in batch:
            selected = entry(value)
            budget[0] -= encoded_size(selected) + 1
            if budget[0] < 0:
                raise ValueError("启动安装元数据超过容量。")
            entries.append(selected)
    return entries


def verify_file(path, digest):
    with path.open("rb") as stream:
        accumulator = hashlib.sha256()
        while chunk := stream.read(65536):
            accumulator.update(chunk)
    if accumulator.hexdigest() != digest:
        raise ImportError("模块内容与安装清单不一致。")


def verified_bytes(path, digest):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ImportError("模块内容与安装清单不一致。")
    return data


class VerifiedLoader:
    def __init__(self, original, path, digest):
        self.original, self.path, self.digest = original, path, digest

    def create_module(self, spec):
        if isinstance(self.original, importlib.machinery.ExtensionFileLoader):
            verify_file(self.path, self.digest)
        return self.original.create_module(spec)

    def exec_module(self, module):
        if isinstance(self.original, importlib.machinery.SourceFileLoader):
            data = verified_bytes(self.path, self.digest)
            exec(compile(data, str(self.path), "exec", dont_inherit=True), module.__dict__)
        else:
            verify_file(self.path, self.digest)
            self.original.exec_module(module)

    def __getattr__(self, name):
        return getattr(self.original, name)


class VerifiedFinder:
    def __init__(self, approved, standard_roots, application_roots):
        self.approved = approved
        self.standard_roots = standard_roots
        self.application_roots = application_roots

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        if spec.origin is None:
            raise ImportError("应用命名空间缺少安装入口。")
        origin = Path(spec.origin).resolve()
        application = any(origin.is_relative_to(root) for root in self.application_roots)
        if not application and any(origin.is_relative_to(root) for root in self.standard_roots):
            return spec
        digest = self.approved.get(origin)
        if digest is None:
            raise ImportError("模块来源未登记在安装清单中。")
        loader = type(spec.loader)(fullname, str(origin))
        spec.origin = str(origin)
        if spec.submodule_search_locations is not None:
            spec.submodule_search_locations[:] = [
                str(Path(location).resolve(strict=True))
                for location in spec.submodule_search_locations
            ]
        spec.loader = VerifiedLoader(loader, origin, digest)
        return spec


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        os.set_inheritable(stream.fileno(), False)
    source = sys.stdin.buffer
    imports = read(source)
    if type(imports) is not dict or set(imports) != {"importPaths"}:
        raise ValueError("模块路径配置无效。")
    paths = imports["importPaths"]
    if type(paths) is not list or not 1 <= len(paths) <= 64:
        raise ValueError("模块路径配置无效。")
    if any(type(path) is not str or not os.path.isabs(path) for path in paths):
        raise ValueError("模块路径必须为绝对路径。")
    config = read(source)
    maximum = config["maxManifestBytes"]
    if type(maximum) is not int or not 1 <= maximum <= 67108864:
        raise ValueError("启动元数据容量无效。")
    states = config["maxComponentStates"]
    if type(states) is not int or not 1 <= states <= 1000000:
        raise ValueError("进程文件状态容量无效。")
    if (
        config["engineInstallation"]["componentCount"]
        + config["parentInstallation"]["componentCount"]
        + 2
        > states
    ):
        raise ValueError("启动文件状态超过容量。")
    budget = [maximum * 2]
    engine_files = manifest(source, config["engineInstallation"], budget)
    parent_files = manifest(source, config["parentInstallation"], budget)
    approved = {}
    for value in (config["engineInstallation"]["executable"], *engine_files):
        if type(value) is not dict or set(value) != {"path", "sha256"}:
            raise ValueError("安装文件字段无效。")
        origin = Path(value["path"]).resolve(strict=True)
        digest = value["sha256"]
        if type(digest) is not str or len(digest) != 64:
            raise ValueError("安装文件摘要无效。")
        verify_file(origin, digest)
        if origin in approved and approved[origin] != digest:
            raise ValueError("安装路径包含冲突摘要。")
        approved[origin] = digest
    standard_roots = tuple(Path(path).resolve() for path in sys.path)
    roots = tuple(Path(path).resolve(strict=True) for path in paths)
    sys.meta_path.insert(0, VerifiedFinder(approved, standard_roots, roots))
    sys.path.extend(str(path) for path in roots)
    sys.dont_write_bytecode = True
    from deskweave.process._worker import main as run

    return run(config, engine_files, parent_files)


if __name__ == "__main__":
    raise SystemExit(main())
