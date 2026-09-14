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

from . import APP_NAME, __version__, config, engine
from .queue import JobQueue
from .updater import Updater

STATIC = Path(__file__).parent / "static"


def make_handler(queue: JobQueue, httpd_ref: dict, updater: Updater):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"fpsconv/{__version__}"

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
                            "version": __version__, "app": APP_NAME, "frozen": engine.FROZEN})
            elif url.path == "/api/update":
                self._json(updater.snapshot())
            elif url.path == "/api/ffmpeg/status":
                self._json(httpd_ref.get("ffmpeg_dl", {"state": "idle"}))
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
                out_dir = data.get("out_dir") or ""
                if not out_dir:
                    return self._json({"error": "choose an output folder first"}, HTTPStatus.BAD_REQUEST)
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
                                  overwrite=data.get("overwrite") or "overwrite")
                self._json({"added": [j.id for j in added]})
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
            elif url.path == "/api/update/check":
                self._json(updater.check())
            elif url.path == "/api/update/download":
                self._json(updater.download_update())
            elif url.path == "/api/update/install":
                self._json(updater.install())
            elif url.path == "/api/dee":
                dee = (data.get("dee_path") or "").strip()
                if not dee:
                    return self._json({"error": "give the path to dee.exe"}, HTTPStatus.BAD_REQUEST)
                if not (os.path.isfile(dee) or __import__("shutil").which(dee)):
                    return self._json({"error": f"not found: {dee}"}, HTTPStatus.BAD_REQUEST)
                path = engine.write_deew_config(dee)
                self._json({"ok": True, "config": str(path), "doctor": engine.doctor()})
            elif url.path == "/api/ffmpeg/download":
                if httpd_ref.get("ffmpeg_dl", {}).get("state") == "running":
                    return self._json({"ok": True, "state": "running"})
                httpd_ref["ffmpeg_dl"] = {"state": "running", "percent": 0, "step": "starting"}

                def work():
                    try:
                        found = engine.download_ffmpeg(
                            lambda p, step: httpd_ref["ffmpeg_dl"].update(percent=round(p), step=step))
                        httpd_ref["ffmpeg_dl"] = {"state": "done", "percent": 100, "found": found}
                    except Exception as exc:  # noqa: BLE001
                        httpd_ref["ffmpeg_dl"] = {"state": "error", "error": str(exc)}

                threading.Thread(target=work, daemon=True).start()
                self._json({"ok": True, "state": "running"})
            elif url.path == "/api/quit":
                self._json({"ok": True, "busy": queue.busy()})
                queue.cancel_all()
                threading.Thread(target=httpd_ref["server"].shutdown, daemon=True).start()
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        # -- api bodies --------------------------------------------------- #

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


def serve(port: int = 8765, open_browser: bool = True, workers: int | None = None,
          native_window: bool | None = None) -> None:
    settings = config.load_settings()
    queue = JobQueue(workers=workers or int(settings.get("workers") or 2))
    port = _free_port(port)
    ref: dict = {}
    def stop() -> None:
        queue.cancel_all()
        threading.Thread(target=ref["server"].shutdown, daemon=True).start()

    updater = Updater(is_busy=queue.busy, on_install=stop)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(queue, ref, updater))
    ref["server"] = httpd
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} {__version__} — GUI at {url}  (Ctrl-C to stop)", flush=True)
    updater.start()

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    use_window = settings.get("native_window", True) if native_window is None else native_window
    shown = False
    if open_browser and use_window:
        from .window import show_window
        shown = show_window(url, on_close=stop)   # blocks until the window closes
    if open_browser and not shown:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        while server_thread.is_alive():
            server_thread.join(0.5)
    except KeyboardInterrupt:
        print("\nStopping — cancelling running jobs.", flush=True)
        queue.cancel_all()
        httpd.shutdown()
    finally:
        httpd.server_close()
