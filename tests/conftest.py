import os
from pathlib import Path
import sys


RUNTIME_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = RUNTIME_DIR / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

os.environ.setdefault("CAD_MODE", "dxf")
# Existing API regression fixtures explicitly exercise the compatibility policy.
# Strict semantic tests override the server setting and subprocess environment.
os.environ.setdefault("CAD_SEMANTIC_POLICY", "legacy")
os.environ.setdefault("CAD_DRAWINGS_DIR", str(RUNTIME_DIR / ".cache" / "pytest" / "drawings"))
os.environ.setdefault("CAD_LOG_DIR", str(RUNTIME_DIR / ".cache" / "pytest" / "logs"))
