# agir-db

PostgreSQL database for tracking PSA's AgIR pipelines.

## What it does

- Tracks file inventory across storage locations
- Identifies missing outputs (pipeline gaps)
- Prevents duplicate processing
- Logs all operations

## Main components

- `ConnectionManager` - database connections
- `PipelineGaps` - finds missing outputs
- `StageStatus` - tracks processing status
- `EventLogger` - operation logging

## Current scope

Seven tables support RAW→JPG conversion. Six additional tables for computer vision stages (detection, segmentation, cutouts, features) not yet implemented.

## Usage

```python
from agir_db import ConnectionManager, PipelineGaps

conn_mgr = ConnectionManager(config_path="config.yaml")
gaps = PipelineGaps(conn_mgr)
missing = gaps.find_missing_outputs()
```