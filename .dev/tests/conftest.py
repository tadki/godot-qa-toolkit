import sys
from pathlib import Path

# .dev/repo-sync scripts import each other as top-level modules
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repo-sync"))
