"""ffn-dl: Cross-platform fanfiction downloader."""

import logging as _logging

__version__ = "1.20.0"

_logger = _logging.getLogger(__name__)

# Portable-build bootstrap. For frozen Windows builds this redirects
# HOME/USERPROFILE into the exe's folder so every library that expands
# ``~`` (BookNLP in particular) lands inside the portable folder rather
# than the user's actual home directory. Runs first so every subsequent
# import sees the corrected environment.
try:
    from . import portable as _portable
    _portable.setup_env()
except Exception:  # never block imports of the main package
    # If this fails, user data lands outside the portable folder — surface
    # the traceback so the failure is diagnosable instead of silent.
    _logger.exception("portable.setup_env() failed; portable layout may be inactive")

# After the portable env is set up, add any user-installed neural
# backends to sys.path so ``import fastcoref`` / ``import booknlp``
# succeed after the user installed them from the GUI.
try:
    from . import neural_env as _neural_env
    _neural_env.activate()
except Exception:
    _logger.exception("neural_env.activate() failed; neural attribution backends unavailable")
