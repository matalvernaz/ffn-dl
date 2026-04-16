"""Allow running as: python -m ffn_dl"""

import sys
import os

# When frozen by PyInstaller, ensure the package is importable
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))

from ffn_dl.cli import main

main()
