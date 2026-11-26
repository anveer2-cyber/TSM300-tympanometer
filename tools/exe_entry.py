"""What TSM300.exe runs. PyInstaller needs a plain script to start from, not the package's ``__main__``."""

import sys

from tsm300.cli import main

sys.exit(main())
