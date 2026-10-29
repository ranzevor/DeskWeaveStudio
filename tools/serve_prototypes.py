"""在本机预览交互画册与设计资料。"""
from __future__ import annotations

import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PreviewHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口范围为 0～65535，0 表示自动选择")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), functools.partial(PreviewHandler, directory=str(ROOT / "docs")))
    print(f"http://127.0.0.1:{server.server_port}/prototypes/index.html#record-config", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
