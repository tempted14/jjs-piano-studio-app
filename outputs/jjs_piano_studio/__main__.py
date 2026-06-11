"""Entry point for python -m jjs_piano_studio."""
import sys
from pathlib import Path

# Ensure the parent directory is on the path
_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

from roblox_piano_macro import main

if __name__ == "__main__":
    main()
