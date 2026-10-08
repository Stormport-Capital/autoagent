"""Put tranche-dashboard/ on sys.path so its modules (engine, indicators,
store, app, data) import unchanged. Import this before any of them."""

import os
import sys

DASH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                     "tranche-dashboard"))
if DASH not in sys.path:
    sys.path.insert(0, DASH)
