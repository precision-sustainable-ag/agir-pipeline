# AGIR-pipeline Structure

## Possible Layout

```
agir-pipeline/
├── pyproject.toml                 # Root project config
├── README.md
├── src/
│   └── agir_db/                   # Core database library (installable package)
│       ├── __init__.py
│       ├── connection.py
│       ├── exceptions.py
│       ├── transfers.py
│       ├── api.py
│       └── ...
│
├── orchestrator/                  # Orchestration system
│   ├── pyproject.toml             # separate deps
│   ├── README.md
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── core.py                # Main orchestration logic
│   │   ├── polling.py             # Readiness polling
│   │   ├── leasing.py             # Work claiming
│   │   ├── submission.py          # Slurm job submission
│   │   ├── ingestion.py           # Result ingestion
│   │   └── retry.py               # Retry/resume logic
│   ├── scripts/
│   │   ├── orch_daemon.py         # Main orchestrator daemon
│   │   └── ...
│   ├── configs/
│   │   └── ...
│   └── tests/
│       └── ...
│
├── stages/                        # Pipeline stage workers
│   ├── README.md
│   ├── common/                    # Shared stage utilities
│   │   ├── __init__.py
│   │   ├── contracts.py           # RunReport, Manifest builders
│   │   ├── utils.py               # Common helpers
│   │   └── parsers.py             # Input validation
│   │
│   ├── raw_to_jpg/                # Stage 1: RAW → JPG
│   │   ├── README.md
│   │   ├── cli.py                 # CLI entry point
│   │   ├── processor.py           # Core conversion logic
│   │   ├── raw_to_jpg.py           # RAW → DNG and DNG → JPG classes
│   │   ├── configs/
│   │   │   └── default.yaml
│   │   └── tests/
│   │       ├── test_processor.py
│   │       └── ...
│   │
│   ├── jpg_to_det/                # Stage 2: JPG → Detections
│   │   └── ...
│   │
│   ├── det_to_seg/                # Stage 3: Detections → Segmentation
│   │   └── ...
│   │
│   └── seg_to_cutouts/            # Stage 4: Segmentation → Cutouts
│       └── ...
│
├── schemas/                       # Shared contracts & DB schemas
│   ├── contracts/
│   │   ├── run_report_schema.json
│   │   └── manifest_schema.json
│   └── sql/
│       ├── source.globus_file_index.sql
│       ├── logs.stage_runs.sql
│       ├── logs.transfer_requests.sql
│       └── report.missing_on_juno.sql
│
├── tests/                         # Old tests
│   ├── test_p1.py
│   └── ...
│
├── scripts/                       # Deployment & maintenance scripts
│   ├── deploy_schemas.sh
│   ├── setup_dev_env.sh
│   └── migrate_data.py
│
└── .github/                       # CI/CD
    └── workflows/
        └── ...
```

## Key Design Principles

### 1. Clear Boundaries, Shared Foundation

```python
# agir_db is the ONLY package that talks to Postgres
from agir_db import AgirDB

# Stages import ONLY shared contracts
from stages.common.contracts import RunReportBuilder, ManifestBuilder

# Orchestrator coordinates but doesn't process
from orchestrator.core import Orchestrator
```

### 2. Dependency Flow

```
orchestrator → agir_db ✓
orchestrator → stages (launches as subprocesses) ✓
stages → agir_db ✗ (stages are DB-agnostic)
stages → stages.common ✓
```

### 3. Installation Patterns

**Development (recommended):**
```bash
# Install all components in editable mode
pip install -e .                      # agir_db from root
pip install -e ./orchestrator         # orchestrator
pip install -e ./stages/raw_to_jpg    # individual stages
```

**Production:**
```bash
# Install specific versions
pip install agir-db==1.2.0
pip install agir-orchestrator==2.0.0
pip install agir-stage-raw-to-jpg==1.5.0
```

### 4. Version Management

**Root `pyproject.toml`:**
```toml
[project]
name = "agir-db"
version = "1.2.0"  # Core library version
```

**Orchestrator `pyproject.toml`:**
```toml
[project]
name = "agir-orchestrator"
version = "2.0.0"  # Orchestrator version
dependencies = [
    "agir-db>=1.2.0,<2.0.0",  # Compatible DB API version
]
```

**Stage `pyproject.toml`:**
```toml
[project]
name = "agir-stage-raw-to-jpg"
version = "1.5.0"  # Stage version (independent)
dependencies = [
    # NO agir-db dependency!
    "rawpy>=0.18.0",
    "pillow>=10.0.0",
]
```

## Deployment Strategy

### Development Environment
```bash
git clone https://github.com/precision-sustainable-ag/agir-pipeline.git
cd agir
pip install -e .[dev]                 # Install agir_db with dev deps
pip install -e ./orchestrator[dev]    # Install orchestrator
pip install -e ./stages/raw_to_jpg    # Install stages as needed
pytest tests/                         # Run all tests
```
