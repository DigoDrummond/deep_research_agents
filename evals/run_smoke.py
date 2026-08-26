"""Cheap WEB-only eval: one lookup (q11, salário mínimo)."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.run_baseline import main

if __name__ == "__main__":
    main(["--smoke"])
