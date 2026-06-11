"""JJS Piano Studio launcher. Run from the outputs/ directory."""
import sys
from pathlib import Path

# Ensure the parent of the jjs_piano_studio package is on the path
_package_root = Path(__file__).resolve().parent
if str(_package_root) not in sys.path:
    sys.path.insert(0, str(_package_root))

from roblox_piano_macro import main

if __name__ == "__main__":
    main()
