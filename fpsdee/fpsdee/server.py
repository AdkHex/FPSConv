"""Local HTTP server: serves the GUI page and a small JSON API for it.

Standard library only. Binds to 127.0.0.1, so nothing outside this machine can
reach it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, engine
from .queue import JobQueue

STATIC = Path(__file__).parent / "static"


def make_handler(queue: JobQueue, httpd_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"fpsdee/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- helpers ------------------------------------------------------ #

        def _json(self, payload, status=HTTPStatus.OK):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        # -- routes ------------------------------------------------------- #

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                body = (STATIC / "index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/status":
                self._json(queue.snapshot())
            elif url.path == "/api/doctor":
                self._json(engine.doctor())
            elif url.path == "/api/settings":
                self._json(config.load_settings())
            elif url.path == "/api/modes":
                self._json({"modes": list(engine.FPS_CONVERSIONS),
                            "ratios": {k: round(v, 6) for k, v in engine.FPS_CONVERSIONS.items()},
                            "version": __version__,
                            "encode": self._encode_options()})
            elif url.path == "/api/browse":
                self._json(self._browse(q.get("path", [""])[0]))
            elif url.path == "/api/probe":
                self._json(self._probe(q.get("path", [""])[0]))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            url = urlparse(self.path)
            data = self._body()
            if url.path == "/api/jobs":
                items = [i for i in data.get("items", []) if i.get("path")]
                mode = data.get("conv_type", "")
                task = data.get("task") or engine.TASK_FPS
                out_dir = data.get("out_dir") or ""
                if not out_dir:
                    return self._json({"error": "choose an output folder first"}, HTTPStatus.BAD_REQUEST)
                if task not in (engine.TASK_FPS, engine.TASK_ENCODE):
                    return self._json({"error": f"unknown task {task!r}"}, HTTPStatus.BAD_REQUEST)
                encode = data.get("encode") or {}
                if task == engine.TASK_ENCODE:
                    err = self._check_encode(encode)
                    if err:
                        return self._json({"error": err}, HTTPStatus.BAD_REQUEST)
                else:
                    if mode not in engine.FPS_CONVERSIONS:
                        return self._json({"error": f"unknown conversion {mode!r}"}, HTTPStatus.BAD_REQUEST)
                    bad = [i["conv_type"] for i in items if i.get("conv_type") and i["conv_type"] not in engine.FPS_CONVERSIONS]
                    if bad:
                        return self._json({"error": f"unknown conversion {bad[0]!r}"}, HTTPStatus.BAD_REQUEST)
                missing = [i["path"] for i in items if not os.path.isfile(i["path"])]
                if missing:
                    return self._json({"error": "file not found: " + "; ".join(missing)}, HTTPStatus.BAD_REQUEST)
                if "workers" in data:
                    queue.set_workers(int(data["workers"]))
                added = queue.add(items, mode, out_dir,
                                  bitrate=int(data.get("bitrate") or 0),
                                  overwrite=data.get("overwrite") or "overwrite",
                                  task=task, encode=encode)
                self._json({"added": [j.id for j in added]})
            elif url.path == "/api/resolve":
                # what each pending stream would become under the current encode settings
                encode = data.get("encode") or {}
                err = self._check_encode(encode)
                if err:
                    return self._json({"error": err}, HTTPStatus.BAD_REQUEST)
                plans = []
                for st in data.get("streams") or []:
                    enc = engine.resolve_encode(encode.get("target") or "ddp", int(encode.get("channels") or 0),
                                                bool(encode.get("atmos", True)), int(encode.get("bitrate") or 0),
                                                int(st.get("channels") or 2), st.get("atmos"), st.get("codec") or "")
                    plans.append(enc.to_dict())
                self._json({"plans": plans})
            elif url.path == "/api/settings":
                self._json(config.save_settings(data))
            elif url.path == "/api/cancel":
                self._json({"ok": queue.cancel(data.get("id", ""))})
            elif url.path == "/api/cancel_all":
                self._json({"cancelled": queue.cancel_all()})
            elif url.path == "/api/retry":
                job = queue.retry(data.get("id", ""))
                self._json({"ok": job is not None, "id": job.id if job else None})
            elif url.path == "/api/clear":
                self._json({"removed": queue.clear_finished()})
            elif url.path == "/api/remove":
                self._json({"ok": queue.remove(data.get("id", ""))})
            elif url.path == "/api/open":
                self._json({"ok": engine.open_folder(data.get("path", ""))})
            elif url.path == "/api/expand":
                folder = data.get("path", "")
                if not os.path.isdir(folder):
                    return self._json({"error": f"not a folder: {folder}"}, HTTPStatus.BAD_REQUEST)
                files = engine.list_audio_files(folder, recursive=bool(data.get("recursive")))
                self._json({"files": files})
            elif url.path == "/api/quit":
                self._json({"ok": True, "busy": queue.busy()})
                queue.cancel_all()
                threading.Thread(target=httpd_ref["server"].shutdown, daemon=True).start()
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        # -- api bodies --------------------------------------------------- #

        @staticmethod
        def _encode_options() -> dict:
            return {
                "targets": list(engine.TARGETS),
                "channels": list(engine.TARGET_CHANNELS),
                "layouts": {str(k): v for k, v in engine.LAYOUT_NAMES.items()},
                "bitrates": {fmt: {str(ch): engine.BITRATES[(fmt, ch)] for ch in (1, 2, 6, 8) if (fmt, ch) in engine.BITRATES}
                             for fmt in engine.TARGETS},
                "atmos_bitrates": engine.ATMOS_BITRATES,
                "defaults": {f"{fmt}_{ch}": kbps for (fmt, ch), kbps in engine.DEFAULT_BITRATE.items()},
                "drc": list(engine.DRC_PROFILES),
            }

        @staticmethod
        def _check_encode(encode: dict) -> str:
            if (encode.get("target") or "ddp") not in engine.TARGETS:
                return f"unknown target {encode.get('target')!r}"
            try:
                ch = int(encode.get("channels") or 0)
            except (TypeError, ValueError):
                return "channels must be 0, 1, 2, 6 or 8"
            if ch not in engine.TARGET_CHANNELS:
                return "channels must be 0, 1, 2, 6 or 8"
            if (encode.get("drc") or "film_light") not in engine.DRC_PROFILES:
                return f"unknown DRC profile {encode.get('drc')!r}"
            return ""

        @staticmethod
        def _browse(path: str) -> dict:
            path = os.path.abspath(os.path.expanduser(path or os.getcwd()))
            if os.path.isfile(path):
                path = os.path.dirname(path)
            if not os.path.isdir(path):
                return {"error": f"not a folder: {path}", "path": path, "entries": []}
            entries = []
            try:
                for name in sorted(os.listdir(path), key=str.lower):
                    if name.startswith("."):
                        continue
                    full = os.path.join(path, name)
                    is_dir = os.path.isdir(full)
                    if is_dir or Path(name).suffix.lower() in engine.AUDIO_EXTS:
                        entries.append({"name": name, "path": full, "dir": is_dir})
            except OSError as exc:
                return {"error": str(exc), "path": path, "entries": []}
            drives = []
            if os.name == "nt":
                import string
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return {"path": path, "parent": os.path.dirname(path), "entries": entries,
                    "drives": drives, "home": str(Path.home())}

        @staticmethod
        def _probe(path: str) -> dict:
            if not os.path.isfile(path):
                return {"error": "file not found"}
            info = engine.probe_streams(path)
            if not info["streams"]:
                return {"error": "no audio stream found"}
            for s in info["streams"]:
                s["engine"] = ("ffmpeg aac" if s["codec"] == "aac"
                               else f"ffmpeg wav → deew {engine.DEE_CODEC_MAP.get(s['codec'], ('', 'ddp', ''))[1]}")
                s["engine_encode"] = "truehdd → DEE Atmos (deezy)" if s["atmos"] else "ffmpeg wav → deew"
            return {
                "path": path, "name": os.path.basename(path),
                "duration": engine.fmt_time(info["duration"]),
                "size": engine.hr_size(os.path.getsize(path)),
                "streams": info["streams"],
            }

    return Handler


def _free_port(preferred: int) -> int:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def serve(port: int = 8765, open_browser: bool = True, workers: int | None = None) -> None:
    settings = config.load_settings()
    queue = JobQueue(workers=workers or int(settings.get("workers") or 2))
    port = _free_port(port)
    ref: dict = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(queue, ref))
    ref["server"] = httpd
    url = f"http://127.0.0.1:{port}/"
    print(f"fpsdee {__version__} — GUI at {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping — cancelling running jobs.", flush=True)
        queue.cancel_all()
    finally:
        httpd.server_close()
