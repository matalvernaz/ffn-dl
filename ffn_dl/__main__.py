"""Entry point for ffn-dl.

- With arguments: runs the CLI  (ffn-dl https://...)
- Without arguments: launches the GUI  (double-click the exe)
"""

import sys
import os

if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))


def main():
    if len(sys.argv) > 1:
        from ffn_dl.cli import main as cli_main
        cli_main()
    else:
        from ffn_dl.gui import main as gui_main
        gui_main()


main()
