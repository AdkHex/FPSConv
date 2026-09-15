"""``python -m fpsdee``            → GUI
``python -m fpsdee convert ...`` → command line fps change (single file or batch)
``python -m fpsdee encode ...``  → command line audio-only encode to DD / DDP / DDP Atmos
``python -m fpsdee doctor``      → what is installed
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import __version__, engine


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
    if args.cmd == "encode":
        queue.add([{"path": s, "stream_index": args.stream} for s in sources], "", args.output,
                  overwrite=args.overwrite, task=engine.TASK_ENCODE,
                  encode={"target": args.format, "channels": args.channels, "bitrate": args.bitrate,
                          "atmos": not args.no_atmos, "drc": args.drc})
    else:
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
        what = f" [{j['pretty']} -> {j['label']}]" if j.get("task") == engine.TASK_ENCODE and j.get("label") else ""
        print(f"{mark} {j['name']}{what} -> {j['out_name'] or '-'} {j['out_size']} {j['error']}")
        if j["state"] not in ("done", "skipped"):
            rc = 1
    return rc


def _cli_doctor() -> int:
    d = engine.doctor()
    print(f"Python      : {d['python']} ({sys.executable})")
    print(f"ffmpeg      : {d['ffmpeg'] or 'NOT FOUND'}")
    print(f"ffprobe     : {d['ffprobe'] or 'NOT FOUND'}")
    print(f"deew        : {'ok via ' + d['deew_python'] if d['deew'] else 'NOT INSTALLED (pip install deew)'}")
    print(f"deew config : {d['deew_config'] or 'not found (run: python -m deew  once to create it, then set dee_path)'}")
    print(f"DEE         : {d['dee'] or 'not configured (dee_path in deew config.toml)'}")
    print(f"mediainfo   : {d['mediainfo'] or 'NOT FOUND (needed to detect Atmos)'}")
    print(f"deezy       : {'ok via ' + d['deezy_cmd'] if d['deezy'] else 'NOT INSTALLED (pip install deezy) — needed for DDP Atmos'}")
    print(f"truehdd     : {d['truehdd'] or 'NOT FOUND — needed for DDP Atmos'}")
    print(f"settings    : {d['config_dir']}")
    ok = d["ffmpeg"] and d["ffprobe"]
    print()
    print("AAC sources need ffmpeg only." if ok else "ffmpeg/ffprobe missing: nothing will work.")
    print("AC-3 / E-AC-3 / TrueHD sources additionally need deew + Dolby Encoding Engine.")
    print("Audio encode to DD / DDP needs deew + DEE; DDP Atmos needs truehdd + deezy + DEE 5.2.x + mediainfo.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fpsdee", description=f"fpsdee {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    gui = sub.add_parser("gui", help="launch the GUI (default)")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true")
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

    enc = sub.add_parser("encode", help="audio-only encode to DD / DDP / DDP Atmos (no fps change)")
    enc.add_argument("inputs", nargs="+", help="files and/or folders")
    enc.add_argument("--format", "-f", choices=list(engine.TARGETS), default="ddp")
    enc.add_argument("--channels", "-c", type=int, choices=list(engine.TARGET_CHANNELS), default=0,
                     help="0 = same as source, else 1 / 2 / 6 (5.1) / 8 (7.1); never upmixes")
    enc.add_argument("--bitrate", "-b", type=int, default=0, help="kbps; 0 = DEE default for the layout")
    enc.add_argument("--no-atmos", action="store_true", help="encode the bed only, even for TrueHD Atmos sources")
    enc.add_argument("--drc", choices=list(engine.DRC_PROFILES), default="film_light")
    enc.add_argument("--output", "-o", default="output")
    enc.add_argument("--jobs", "-j", type=int, default=1)
    enc.add_argument("--recursive", "-r", action="store_true")
    enc.add_argument("--stream", "-s", type=int, default=0, help="audio stream index (0 = first)")
    enc.add_argument("--overwrite", choices=["overwrite", "skip", "rename"], default="overwrite")

    sub.add_parser("doctor", help="show what is installed")

    args = parser.parse_args(argv)
    if args.cmd in ("convert", "encode"):
        return _cli_convert(args)
    if args.cmd == "doctor":
        return _cli_doctor()
    from .server import serve

    serve(port=getattr(args, "port", 8765), open_browser=not getattr(args, "no_browser", False),
          workers=getattr(args, "jobs", None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
