"""Entry point: `streamlit run streamlit_app.py`."""

import sys
from pathlib import Path

# Worktree self-loading: when this file lives in a worktree whose ``src/`` is
# what the user actually wants to run — not the main checkout's editable
# install — prepend it to sys.path so ``import wdmigrator`` picks up this
# tree's code, not whatever the venv was linked at.
_worktree_src = Path(__file__).resolve().parent / "src"
if _worktree_src.is_dir():
    sys.path.insert(0, str(_worktree_src))

from wdmigrator.ui.app import main

main()
