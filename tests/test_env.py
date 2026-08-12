#!/usr/bin/env python3
"""
AGIR Environment Smoke Test

Run:
  python tests/test_env.py
  # or (recommended)
  uv run python tests/test_env.py

Exit codes:
  0 = all checks passed
  1 = one or more checks failed

Notes:
- This script only imports packages and performs tiny "sanity" operations.
- It avoids GPU assumptions (PyTorch CUDA is checked but not required).
"""

from __future__ import annotations

import importlib
import os
import platform
import sqlite3
import shutil
import subprocess
import sys
import textwrap
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass
class Check:
    name: str
    func: Callable[[], None]
    required: bool = True


class Colors:
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    NC = "\033[0m"


def ok(msg: str) -> None:
    print(f"{Colors.GREEN}✓{Colors.NC} {msg}")


def warn(msg: str) -> None:
    print(f"{Colors.YELLOW}!{Colors.NC} {msg}")


def fail(msg: str) -> None:
    print(f"{Colors.RED}✗{Colors.NC} {msg}")


def header(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def require_python_312() -> None:
    if sys.version_info < (3, 12):
        raise RuntimeError(f"Python 3.12+ required, found {sys.version.split()[0]}")
    ok(f"Python version: {sys.version.split()[0]}")


def print_env_info() -> None:
    ok(f"Executable: {sys.executable}")
    ok(f"Platform: {platform.platform()}")
    ok(f"Prefix: {sys.prefix}")
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        ok(f"VIRTUAL_ENV: {venv}")
    else:
        warn("VIRTUAL_ENV not set (you might not be activated; that's OK if using explicit python path).")


def import_and_version(mod: str, attr: str = "__version__") -> str:
    m = importlib.import_module(mod)
    ver = getattr(m, attr, None)
    return str(ver) if ver is not None else "unknown"


def check_pyyaml() -> None:
    import yaml  # type: ignore

    data = yaml.safe_load("a: 1\nb: [2, 3]\n")
    assert data["a"] == 1 and data["b"] == [2, 3]
    ok(f"pyyaml import OK ({import_and_version('yaml')})")


def check_sqlite_schema() -> None:
    from orchestrator.sqlite_db import open_db

    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "sqlite" / "pipeline.sql"
    schema = schema_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    required_tables = {"globus_file_index", "stage_leases", "stage_runs", "staged_inputs"}
    missing = required_tables - tables
    assert not missing, f"SQLite schema is missing required tables: {sorted(missing)}"
    assert callable(open_db)
    ok(f"SQLite schema loaded successfully ({sqlite3.sqlite_version})")


def check_numpy() -> None:
    import numpy as np  # type: ignore

    x = np.array([1, 2, 3], dtype=np.float32)
    print(x)
    assert float(x.mean()) == 2.0
    ok(f"numpy import OK ({np.__version__})")


def check_pillow() -> None:
    from PIL import Image  # type: ignore

    img = Image.new("RGB", (16, 16), (10, 20, 30))
    assert img.size == (16, 16)
    ok(f"Pillow import OK ({import_and_version('PIL', 'PILLOW_VERSION') if hasattr(importlib.import_module('PIL'), 'PILLOW_VERSION') else import_and_version('PIL')})")


def check_scikit_image() -> None:
    import numpy as np  # type: ignore
    from skimage import filters  # type: ignore

    img = np.zeros((32, 32), dtype=np.float32)
    img[8:24, 8:24] = 1.0
    edges = filters.sobel(img)
    assert edges.shape == img.shape
    ok(f"scikit-image import OK ({import_and_version('skimage')})")


def check_opencv() -> None:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[8:24, 8:24] = (0, 255, 0)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    assert gray.shape == (32, 32)
    ok(f"opencv import OK ({cv2.__version__})")


def check_pidng() -> None:
    # pidng may not have a consistent version attribute; just import
    import pidng  # type: ignore  # noqa: F401

    ok("pidng import OK")


def check_torch() -> None:
    import torch  # type: ignore

    x = torch.tensor([1.0, 2.0, 3.0])
    assert float(x.mean()) == 2.0
    ok(f"torch import OK ({torch.__version__})")

    # CUDA is optional
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="CUDA initialization: Unexpected error from cudaGetDeviceCount.*",
            category=UserWarning,
        )
        cuda_available = torch.cuda.is_available()

    if cuda_available:
        ok(f"torch CUDA available: {torch.version.cuda}")
    else:
        warn("torch CUDA not available (OK if you intend CPU-only).")


def check_torchvision() -> None:
    import torch  # type: ignore
    import torchvision  # type: ignore
    from torchvision import transforms  # type: ignore

    t = transforms.Compose([transforms.Resize((16, 16)), transforms.ToTensor()])
    # create a trivial PIL image to transform
    from PIL import Image  # type: ignore

    img = Image.new("RGB", (32, 32), (123, 50, 10))
    out = t(img)
    assert isinstance(out, torch.Tensor)
    assert tuple(out.shape) == (3, 16, 16)
    ok(f"torchvision import OK ({torchvision.__version__})")


def check_ultralytics() -> None:
    # Ultralytics import should not force download; we only import.
    import ultralytics  # type: ignore

    ok(f"ultralytics import OK ({getattr(ultralytics, '__version__', 'unknown')})")


def main() -> int:
    header("AGIR Environment Smoke Test")
    require_python_312()
    print_env_info()

    checks: list[Check] = [
        Check("pyyaml", check_pyyaml, required=True),
        Check("sqlite schema", check_sqlite_schema, required=True),

        # Optional groups (if you installed extras)
        Check("numpy", check_numpy, required=False),
        Check("pillow", check_pillow, required=False),
        Check("scikit-image", check_scikit_image, required=False),
        Check("opencv-python", check_opencv, required=False),
        Check("pidng", check_pidng, required=False),

        Check("torch", check_torch, required=False),
        Check("torchvision", check_torchvision, required=False),
        Check("ultralytics", check_ultralytics, required=False),
    ]

    failures = 0

    header("Running checks")
    for c in checks:
        try:
            c.func()
        except ModuleNotFoundError as e:
            if c.required:
                fail(f"{c.name}: missing ({e})")
                failures += 1
            else:
                warn(f"{c.name}: not installed (optional) — {e}")
        except Exception as e:
            if c.required:
                fail(f"{c.name}: FAILED — {e}")
                failures += 1
            else:
                warn(f"{c.name}: optional check failed — {e}")

    header("Summary")
    if failures == 0:
        ok("All required checks passed.")
        print("\nOptional packages may be skipped if you didn't install those extras.")
        return 0

    fail(f"{failures} required check(s) failed.")
    print(
        textwrap.dedent(
            """
            Tips:
              - Confirm you're using the intended venv:
                  which python
                  python -c "import sys; print(sys.prefix)"
              - If using uv:
                  uv pip install -e ".[dev]"
                  uv pip install -e ".[all]"
              - If the SQLite check failed, confirm schemas/sqlite/pipeline.sql is present.
            """
        ).strip()
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
