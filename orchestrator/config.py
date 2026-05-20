# orchestrator/config.py
"""
Shared config loading for orchestrator scripts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml


def load_stage_config(config_path: str | Path) -> Dict:
    """Load a stage config YAML and return the parsed dict.

    Used by both stage_inputs and submit_jobs — the config file format
    is shared, so loading should be too.
    """
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)