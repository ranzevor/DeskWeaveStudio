"""以 RFC 8785 官方数值与独立 JavaScript 检查规范字节。"""
from pathlib import Path
import hashlib
import json
import struct
from canonical_json import dumps, loads

ROOT = Path(__file__).resolve().parents[1]


def main():
    rows = json.loads((ROOT / "examples/canonical/vectors.json").read_text(encoding="utf-8"))["vectors"]
    accepted = rejected = 0
    for row in rows:
        try:
            value = struct.unpack(">d", bytes.fromhex(row["float64Hex"]))[0] if "float64Hex" in row else loads(row["inputJson"])
            actual = dumps(value)
        except (ValueError, OverflowError):
            assert row.get("reject"), row["name"]
            rejected += 1
            continue
        assert not row.get("reject"), row["name"]
        assert actual.decode("utf-8") == row["canonical"], row["name"]
        assert hashlib.sha256(actual).hexdigest() == row["sha256"], row["name"]
        accepted += 1
    print(f"Python JCS：{accepted} 组规范字节、{rejected} 组拒绝向量通过")


if __name__ == "__main__":
    main()
