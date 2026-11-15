"""Start ClipEditor as an app (double-click): no console window, its own window.
Closing the window quits ClipEditor."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import main  # noqa: E402

main(app_mode=True)
