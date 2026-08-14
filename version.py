"""The app version, read once from the VERSION file.

Kept in a plain text file rather than a Python constant so the update script can
compare "what you have" against "what the repo has" without importing the app.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")

FALLBACK = "0.0.0"


def read_version():
    """Never raises: a missing/unreadable VERSION file just reports 0.0.0 rather
    than taking down the app over a cosmetic string."""
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or FALLBACK
    except OSError:
        return FALLBACK


__version__ = read_version()
