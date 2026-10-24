"""Read-only, CORS-enabled static feed server for local frontend development."""

import argparse
import gzip
from io import BytesIO
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self._head_only = False
        self._serve()

    def _serve(self):
        path = Path(self.translate_path(self.path))
        root = Path(self.directory).resolve()
        if (not path.resolve().is_relative_to(root) or path.is_dir()
                or any(part.startswith(".") for part in path.relative_to(root).parts)
                or not path.name.endswith((".json", ".bin.gz", ".wvp"))):
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        # Use identical path validation for both supported methods.
        self._head_only = True
        self._serve()

    def copyfile(self, source, outputfile):
        if getattr(self, "_head_only", False):
            return
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            super().copyfile(source, outputfile)
        else:
            while remaining:
                data = source.read(min(64 * 1024, remaining))
                if not data:
                    break
                outputfile.write(data)
                remaining -= len(data)

    def send_head(self):
        self._remaining = None
        path = Path(self.translate_path(self.path))
        if path.suffix == ".wvp" and path.is_file():
            size = path.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", requested)
                if not match or not 0 <= int(match[1]) <= int(match[2]) < size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                start, end = map(int, match.groups())
            f = path.open("rb")
            f.seek(start)
            self._remaining = end - start + 1
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(self._remaining))
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            return f
        if path.suffix == ".json" and path.is_file() and re.search(r"(?:^|,)\s*gzip(?:\s*;\s*q=(?!0(?:[.,]|$))[^,]+)?\s*(?:,|$)", self.headers.get("Accept-Encoding", "")):
            payload = gzip.compress(path.read_bytes(), compresslevel=3, mtime=0)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return BytesIO(payload)
        return super().send_head()

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_status", 200) >= 400 or self.path.split("?")[0].endswith(("latest.json", "benchmark.json", "run-stats.json", "packing-results.json")):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    handler = partial(Handler, directory=str(root))
    print(f"W prototype feed: http://{args.bind}:{args.port}/latest.json", flush=True)
    ThreadingHTTPServer((args.bind, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
