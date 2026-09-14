"""``FPSConv`` / ``python -m fpsconv``   → GUI
``… convert …``                        → command line (single file or batch)
``… doctor``                           → what is installed
``… deew …``                           → run the bundled deew (used by the installed build)
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import time

from . import APP_NAME, __version__, engine


def _run_bundled_deew(argv: list[str]) -> int:
    """The installed Windows build carries deew inside the exe; this runs it."""
    sys.argv = ["deew", *argv]
    try:
        runpy.run_module("deew", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _cli_convert(args: argparse.Namespace) -> int:
    from .queue import JobQueue

    sources: list[str] = []
    for p in args.inputs:
        if os.path.isdir(p):
            sources.extend(engine.list_audio_files(p, recursive=args.recursive))
        else:
            sources.append(p)
    if not sources:
        print("No input files.", file=sys.stderr)
        return 1

    queue = JobQueue(workers=args.jobs, history=False)
    queue.add([{"path": s, "stream_index": args.stream} for s in sources], args.mode,
              args.output, bitrate=args.bitrate, overwrite=args.overwrite)
    last = ""
    try:
        while True:
            snap = queue.snapshot()
            line = " | ".join(
                f"{j['name'][:28]} {j['step']} {j['percent']:.0f}%" for j in snap["jobs"]
                if j["state"] == "running"
            )
            if line and line != last:
                print("\r" + line[:120].ljust(120), end="", flush=True)
                last = line
            if all(j["state"] not in ("queued", "running") for j in snap["jobs"]):
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        queue.cancel_all()
        print("\ncancelled")
        return 130
    print()
    rc = 0
    for j in queue.snapshot()["jobs"]:
        mark = {"done": "done   ", "failed": "FAILED ", "cancelled": "cancel ", "skipped": "skipped"}[j["state"]]
        print(f"{mark} {j['name']} -> {j['out_name'] or '-'} {j['out_size']} {j['error']}")
        if j["state"] not in ("done", "skipped"):
            rc = 1
    return rc


def _cli_doctor() -> int:
    d = engine.doctor()
    print(f"{APP_NAME} {__version__}  ({'installed build' if d['frozen'] else 'from source'})")
    print(f"Python      : {d['python']} ({sys.executable})")
    print(f"ffmpeg      : {d['ffmpeg'] or 'NOT FOUND'}")
    print(f"ffprobe     : {d['ffprobe'] or 'NOT FOUND'}")
    print(f"deew        : {'ok (' + d['deew_via'] + ')' if d['deew'] else 'NOT AVAILABLE (pip install deew, or use the installed build)'}")
    print(f"deew config : {d['deew_config'] or 'not written yet (set the DEE path in Settings, or: dee <path>)'}")
    print(f"DEE         : {d['dee'] or 'not found' + (' at ' + d['dee_path'] if d['dee_path'] else '')}")
    print(f"settings    : {d['config_dir']}")
    ok = d["ffmpeg"] and d["ffprobe"]
    print()
    print("AAC sources need ffmpeg only." if ok else "ffmpeg/ffprobe missing: nothing will work.")
    print("AC-3 / E-AC-3 / TrueHD sources additionally need deew + Dolby Encoding Engine.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "deew":
        return _run_bundled_deew(argv[1:])

    parser = argparse.ArgumentParser(prog="fpsconv", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    gui = sub.add_parser("gui", help="launch the GUI (default)")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true", help="serve only; open the URL yourself")
    gui.add_argument("--browser", action="store_true", help="use the default browser instead of a window")
    gui.add_argument("--jobs", "-j", type=int, default=None, help="parallel jobs (default: saved setting)")

    conv = sub.add_parser("convert", help="convert files or folders from the command line")
    conv.add_argument("inputs", nargs="+", help="files and/or folders")
    conv.add_argument("--mode", "-m", required=True, choices=list(engine.FPS_CONVERSIONS))
    conv.add_argument("--output", "-o", default="output")
    conv.add_argument("--jobs", "-j", type=int, default=1)
    conv.add_argument("--recursive", "-r", action="store_true")
    conv.add_argument("--stream", "-s", type=int, default=0, help="audio stream index (0 = first)")
    conv.add_argument("--bitrate", "-b", type=int, default=0, help="kbps; 0 = keep the source bitrate")
    conv.add_argument("--overwrite", choices=["overwrite", "skip", "rename"], default="overwrite")

    sub.add_parser("doctor", help="show what is installed")

    dee = sub.add_parser("dee", help="write deew's config.toml for a Dolby Encoding Engine path")
    dee.add_argument("path", help=r"path to dee.exe, e.g. C:\Dolby\DEE\dee.exe")

    args = parser.parse_args(argv)
    if args.cmd == "convert":
        return _cli_convert(args)
    if args.cmd == "doctor":
        return _cli_doctor()
    if args.cmd == "dee":
        print(f"wrote {engine.write_deew_config(args.path)}")
        return _cli_doctor()
    from .server import serve

    serve(port=getattr(args, "port", 8765), open_browser=not getattr(args, "no_browser", False),
          workers=getattr(args, "jobs", None),
          native_window=False if getattr(args, "browser", False) else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
