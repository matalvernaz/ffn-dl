"""ffn-dl: Cross-platform fanfiction downloader."""

__version__ = "1.16.1"

# Portable-build bootstrap. For frozen Windows builds this redirects
# HOME/USERPROFILE into the exe's folder so every library that expands
# ``~`` (BookNLP in particular) lands inside the portable folder rather
# than the user's actual home directory. Runs first so every subsequent
# import sees the corrected environment.
try:
    from . import portable as _portable
    _portable.setup_env()
except Exception:  # never block imports of the main package
    pass

# After the portable env is set up, add any user-installed neural
# backends to sys.path so ``import fastcoref`` / ``import booknlp``
# succeed after the user installed them from the GUI.
try:
    from . import neural_env as _neural_env
    _neural_env.activate()
except Exception:
    pass
