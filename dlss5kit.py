"""Entry point so `python dlss5kit.py` and the built exe both work."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dlss5kit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
