"""``FPSConv`` / ``python -m fpsconv``   → GUI
``… convert …``                        → command line fps change (single file or batch)
``… encode …``                         → command line audio-only encode to DD / DDP / DDP Atmos
``… doctor``                           → what is installed
``… deew …`` / ``… deezy …``           → run the bundled deew / deezy (used by the installed build)
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import time

from . import APP_NAME, __version__, engine
from . import log as applog


def _hide_child_windows() -> None:
    """deew starts dee.exe / ffmpeg with plain Popen; on Windows that would open
    a console window for each. Give every child CREATE_NO_WINDOW + SW_HIDE."""
    if sys.platform != "win32":
        return
    import subprocess

    original = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        si = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched  # type: ignore[method-assign]


def _run_bundled(module: str, argv: list[str]) -> int:
    """The installed Windows build carries deew and deezy inside the exe; this runs one."""
    _hide_child_windows()
    sys.argv = [module, *argv]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException:  # noqa: BLE001 - report on stderr, never a Windows error dialog
        import traceback
        traceback.print_exc()
        sys.stderr.flush()
        return 1
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


def _cli_probe(args) -> int:
    """Show every audio stream and the frame rate each file is tied to.

    With ``--target VIDEO`` it also says which conversion makes the audio fit
    that video (from the frame rates, or from the durations if the audio-only
    file carries no fps).
    """
    ref = None
    if args.target:
        ref = engine.probe_video(args.target)
        print(f"target : {ref['name']}  {ref['fps'] or '?'} fps ({ref['fps_source'] or 'unknown'})  {ref['duration']}")
        print()
    rc = 0
    for path in args.inputs:
        info = engine.probe_streams(path)
        if not info["streams"]:
            print(f"{os.path.basename(path)}: no audio stream found")
            rc = 1
            continue
        fps = f"{info['fps']} fps ({info['fps_source']})" if info["fps"] else "fps unknown (audio only – use --target)"
        print(f"{os.path.basename(path)}  {engine.fmt_time(info['duration'])}  {fps}")
        for st in info["streams"]:
            atmos = "" if st["atmos"] is not None else "  (Atmos: unknown, mediainfo missing)" if st["codec"] == "truehd" else ""
            print(f"    #{st['index']} {st['pretty']} {st['bitrate']} kbps {st['channels']} ch {st['sample_rate']} Hz"
                  f"{' ' + st['language'] if st['language'] else ''}{' – ' + st['title'] if st['title'] else ''}{atmos}")
        if ref:
            sg = engine.suggest_conversion(info["fps"], info["duration"], ref["fps"], ref["duration_s"])
            if sg is None:
                print("    suggestion: none – durations do not match any known conversion")
            elif sg["conv_type"]:
                print(f"    suggestion: --mode {sg['conv_type']}   ({sg['reason']}{', off by ' + str(sg['delta']) + ' s' if sg['delta'] else ''})")
            else:
                print(f"    suggestion: {sg['reason']}")
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
    print(f"mediainfo   : {d['mediainfo'] or ('pymediainfo (bundled)' if d['pymediainfo'] else 'NOT FOUND (needed to detect Atmos)')}")
    print(f"deezy       : {'ok (' + d['deezy_via'] + ')' if d['deezy'] else 'NOT FOUND — needed for DDP Atmos: DeeZy standalone exe (github.com/jessielw/DeeZy), set its path in Settings'}")
    print(f"truehdd     : {d['truehdd'] or 'NOT FOUND — needed for DDP Atmos (github.com/truehdd/truehdd)'}")
    print(f"settings    : {d['config_dir']}")
    ok = d["ffmpeg"] and d["ffprobe"]
    print()
    print("AAC sources need ffmpeg only." if ok else "ffmpeg/ffprobe missing: nothing will work.")
    print("AC-3 / E-AC-3 / TrueHD sources additionally need deew + Dolby Encoding Engine.")
    print("Audio encode to DD / DDP needs DEE; DDP Atmos additionally needs truehdd + deezy + DEE 5.2.x.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("deew", "deezy"):
        return _run_bundled(argv[0], argv[1:])

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

    probe = sub.add_parser("probe", help="show audio streams and the frame rate a file is tied to")
    probe.add_argument("inputs", nargs="+", help="audio / video files")
    probe.add_argument("--target", "-t", help="video the audio must fit; suggests the conversion")

    sub.add_parser("doctor", help="show what is installed")

    dee = sub.add_parser("dee", help="write deew's config.toml for a Dolby Encoding Engine path")
    dee.add_argument("path", help=r"path to dee.exe, e.g. C:\Dolby\DEE\dee.exe")

    args = parser.parse_args(argv)
    if args.cmd in ("convert", "encode"):
        return _cli_convert(args)
    if args.cmd == "probe":
        return _cli_probe(args)
    if args.cmd == "doctor":
        return _cli_doctor()
    if args.cmd == "dee":
        print(f"wrote {engine.write_deew_config(args.path)}")
        return _cli_doctor()
    from .server import serve

    applog.setup()
    serve(port=getattr(args, "port", 8765), open_browser=not getattr(args, "no_browser", False),
          workers=getattr(args, "jobs", None),
          native_window=False if getattr(args, "browser", False) else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
