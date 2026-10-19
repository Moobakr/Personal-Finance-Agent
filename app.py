"""Run with: python -m streamlit run app.py"""
from pathlib import Path
import sys

# Streamlit runners do not all add the script directory to Python's import path.
# Resolve from this file so absolute-path launches also work from other folders.
project_root = str(Path(__file__).resolve().parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from finance_agent.ui import main

if __name__ == "__main__":
    main()
