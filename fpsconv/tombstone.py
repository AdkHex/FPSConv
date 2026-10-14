"""FPSConv is retired: every launch shows an empty black window and nothing else.

The updater keeps running in the background with auto-install forced on, so a
later release can still replace this build. No server, no jobs, no tools.
"""

from __future__ import annotations

from . import APP_NAME, config, updater

BLACK_HTML = "<!doctype html><html style=\"background:#000;margin:0;height:100%\"><body style=\"background:#000;margin:0\"></body></html>"


def run() -> int:
    try:
        config.save_settings({"auto_update": True})
    except Exception:  # noqa: BLE001 - a read-only profile must not stop the black screen
        pass

    window = None

    def close() -> None:
        if window is not None:
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

    upd = updater.Updater(is_busy=lambda: False, on_install=close)
    upd.start()

    try:
        import webview  # pywebview
    except Exception:  # noqa: BLE001
        return 0
    try:
        window = webview.create_window(
            APP_NAME, html=BLACK_HTML,
            width=1320, height=860, min_size=(960, 640), background_color="#000000",
        )
        webview.start()
    except Exception:  # noqa: BLE001 - e.g. no WebView2 runtime
        pass
    return 0
