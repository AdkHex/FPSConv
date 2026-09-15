from __future__ import annotations

import sys


def main() -> int:
    try:
        from app import main as gui_main
    except ImportError as exc:
        print("Failed to start GUI application.")
        print("Check your Python installation and required modules.")
        print(f"Details: {exc}")
        return 1
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
