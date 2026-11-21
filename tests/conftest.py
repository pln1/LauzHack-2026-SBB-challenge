import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("GEMINI_API_KEY", "")
