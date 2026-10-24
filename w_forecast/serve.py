"""Read-only, CORS-enabled static feed server for local frontend development."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        self._head_only = False
        self._serve()

    def _serve(self):
        path = Path(self.translate_path(self.path))
        root = Path(self.directory).resolve()
        if (not path.resolve().is_relative_to(root) or path.is_dir()
                or any(part.startswith(".") for part in path.relative_to(root).parts)
                or not (path.name.endswith(".json") or path.name.endswith(".bin.gz"))):
            self.send_error(404)
            return
        super().do_GET()

    def do_HEAD(self):
        # Use identical path validation for both supported methods.
        self._head_only = True
        self._serve()

    def copyfile(self, source, outputfile):
        if not getattr(self, "_head_only", False):
            super().copyfile(source, outputfile)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.path.split("?")[0].endswith(("latest.json", "benchmark.json")):
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
