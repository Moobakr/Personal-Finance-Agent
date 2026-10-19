"""Run the local regression suite; no live API key or requests required."""
from pathlib import Path
import sys
import unittest

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(root / "tests"), top_level_dir=str(root)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
