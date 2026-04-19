"""Pure helpers consumed by the GUI dialogs.

Kept separate from ``library/gui.py`` so tests can cover the display
rules without pulling in wxPython, which is an optional dependency.
"""

from __future__ import annotations

from pathlib import Path

from .reorganizer import MoveOp


def relative_to_root(p: Path, root: Path) -> str:
    """Display-friendly form: relative to the library root when under
    it, absolute otherwise."""
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def format_move_label(op: MoveOp, root: Path, checked: bool) -> str:
    """The string shown in a ReorganizePreviewDialog row.

    ``[x] `` / ``[ ] `` prefix matches the pattern used elsewhere in
    the GUI (StoryPickerDialog) for NVDA state reporting on
    ``wx.CheckListBox``, which doesn't report check state reliably
    through MSAA on Windows.
    """
    prefix = "[x] " if checked else "[ ] "
    source = relative_to_root(op.source, root)
    target = relative_to_root(op.target, root)
    arrow = "renamed to" if op.is_rename else "→"
    return f"{prefix}{source}  {arrow}  {target}"
