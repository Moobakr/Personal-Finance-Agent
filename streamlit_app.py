"""Public Community Cloud demo; personal desktop usage stays in app.py."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from finance_agent.ui import main

if __name__ == "__main__":
    main(demo=True)
