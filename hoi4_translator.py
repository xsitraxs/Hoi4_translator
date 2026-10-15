"""Entry point: ``python hoi4_translator.py`` starts the GUI.

The implementation now lives in the :mod:`hoi4translator` package; this module
only re-exports its public names, so ``import hoi4_translator`` keeps working
for scripts, tests and the packaged build.
"""
from hoi4translator import *  # noqa: F401,F403  (public API re-export)
from hoi4translator import USAGE, __version__, main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
