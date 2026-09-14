"""Optional native desktop window around the local GUI (pywebview).

On Windows this uses the Edge WebView2 runtime that ships with Windows 10/11,
so the app looks and behaves like a normal desktop program — one window, no
browser tab.  If pywebview is missing or the platform has no web view, the
caller falls back to the default browser; nothing else changes.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import APP_NAME, __version__

_window = None
AUDIO_FILTER = ("Audio / video (*.mka;*.mkv;*.mp4;*.m4a;*.mov;*.ts;*.m2ts;*.webm;*.ac3;*.ec3;*.eac3;"
                "*.thd;*.truehd;*.aac;*.wav;*.flac;*.ogg;*.opus)", "All files (*.*)")


def has_window() -> bool:
    return _window is not None


def pick(kind: str, start: str = "") -> Optional[list[str]]:
    """Native OS dialog through pywebview. ``None`` when there is no window."""
    if _window is None:
        return None
    import webview

    try:
        FD = getattr(webview, "FileDialog", None)
        if kind == "folder":
            dtype = FD.FOLDER if FD else webview.FOLDER_DIALOG
            res = _window.create_file_dialog(dtype, directory=start or "")
        else:
            dtype = FD.OPEN if FD else webview.OPEN_DIALOG
            res = _window.create_file_dialog(dtype, directory=start or "", allow_multiple=True,
                                             file_types=AUDIO_FILTER)
    except Exception:  # noqa: BLE001
        return None
    if not res:
        return []
    return [str(p) for p in (res if isinstance(res, (list, tuple)) else [res])]


def show_window(url: str, on_close: Callable[[], None]) -> bool:
    """Open ``url`` in a native window and block until it is closed.

    Returns ``False`` immediately if no native window could be created.
    """
    try:
        import webview  # pywebview
    except Exception:  # noqa: BLE001
        return False
    global _window
    try:
        window = webview.create_window(
            f"{APP_NAME} {__version__}".strip(), url,
            width=1320, height=860, min_size=(960, 640), background_color="#141414",
        )
        window.events.closed += on_close
        _window = window
        webview.start()
        _window = None
        return True
    except Exception:  # noqa: BLE001 - e.g. no WebView2 runtime
        return False
