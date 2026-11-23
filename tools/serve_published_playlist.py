#!/usr/bin/env python3
"""Serve a published playlist locally; production should use a managed HTTPS host."""
from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os


class PlaylistHandler(SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def guess_type(self, path):
        if path.endswith((".m3u", ".m3u8")):
            return "application/vnd.apple.mpegurl; charset=utf-8"
        return super().guess_type(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"Publish root no existe: {root}")
    os.chdir(root)
    server = ThreadingHTTPServer((args.host, args.port), PlaylistHandler)
    print(f"Listening on http://{args.host}:{args.port}/iptv/unificada.m3u", flush=True)
    server.serve_forever()
