"""Frozen entry point (PyInstaller). Equivalent to ``python -m fpsconv``."""

import multiprocessing
import sys

from fpsconv.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
