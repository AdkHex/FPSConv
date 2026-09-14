"""Optional native desktop window around the local GUI (pywebview).

On Windows this uses the Edge WebView2 runtime that ships with Windows 10/11,
so the app looks and behaves like a normal desktop program — one window, no
browser tab.  If pywebview is missing or the platform has no web view, the
caller falls back to the default browser; nothing else changes.
"""

from __future__ import annotations

from typing import Callable

from . import APP_NAME, __version__


def show_window(url: str, on_close: Callable[[], None]) -> bool:
    """Open ``url`` in a native window and block until it is closed.

    Returns ``False`` immediately if no native window could be created.
    """
    try:
        import webview  # pywebview
    except Exception:  # noqa: BLE001
        return False
    try:
        window = webview.create_window(
            f"{APP_NAME} {__version__}".strip(), url,
            width=1280, height=820, min_size=(960, 640), background_color="#141414",
        )
        window.events.closed += on_close
        webview.start()
        return True
    except Exception:  # noqa: BLE001 - e.g. no WebView2 runtime
        return False
