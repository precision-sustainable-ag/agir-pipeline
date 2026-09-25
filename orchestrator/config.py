# orchestrator/config.py
"""
Shared config loading for orchestrator scripts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# paths.* keys resolved against agir_pipeline_dir when relative.
REPO_RELATIVE_PATH_KEYS = ("uv_env", "stage_config", "log_dir", "script_dir")


def load_stage_config(config_path: str | Path) -> Dict:
    """Load a stage config YAML and return the parsed dict.

    Used by both stage_inputs and submit_jobs — the config file format
    is shared, so loading should be too.

    ``paths.agir_pipeline_dir`` defaults to this checkout.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    paths = cfg.get("paths") if isinstance(cfg, dict) else None
    if isinstance(paths, dict):
        agir_dir = Path(paths.get("agir_pipeline_dir") or REPO_ROOT)
        paths["agir_pipeline_dir"] = str(agir_dir)
        for key in REPO_RELATIVE_PATH_KEYS:
            if paths.get(key):
                paths[key] = str(agir_dir / paths[key])
    return cfg
