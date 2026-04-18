"""ffn-dl: Cross-platform fanfiction downloader."""

__version__ = "1.9.2"

# When running inside the frozen Windows .exe, neural attribution
# backends live in a user-writable directory outside the PyInstaller
# bundle (see ffn_dl.neural_env). Activate that directory early so
# `import fastcoref` / `import booknlp` work after a user has
# installed them from the GUI.
try:
    from . import neural_env as _neural_env
    _neural_env.activate()
except Exception:  # never block imports of the main package
    pass
