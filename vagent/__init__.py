import os
from pathlib import Path

__version__ = "0.1.0"
ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
# keep YoWASP's compiled-WebAssembly cache inside the project
os.environ.setdefault("YOWASP_CACHE_DIR", str(ROOT / ".yowasp_cache"))
