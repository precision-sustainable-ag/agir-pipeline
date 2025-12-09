
```sh
agir-db/
├── src/agir_db/
│   ├── __init__.py                 # Export AgirDB + exceptions
│   ├── api.py                      # AgirDB facade (~150 lines)
│   ├── connection.py               # ConnectionManager (~150 lines)
│   ├── gaps.py                     # PipelineGaps (~250 lines)
│   ├── stages.py                   # StageStatus (~250 lines)
│   ├── images.py                   # ImageMetadata (~350 lines)
│   ├── transfers.py                # TransferManager (~300 lines)
│   ├── events.py                   # EventLogger (~250 lines)
│   ├── inventory.py                # InventorySync (~150 lines)
│   ├── analytics.py                # Analytics (~200 lines)
│   ├── migration.py                # Migration (~250 lines)
│   ├── batches.py                  # BatchMetadata (~200 lines)
│   ├── exceptions.py               # Custom exceptions (~100 lines)
│   └── utils/
│       ├── __init__.py
│       ├── db.py                   # Existing - keep as-is
│       └── logging_setup.py        # Logging configuration (~100 lines)
│
├── sql/
│   ├── schemas/
│   │   ├── 00_init/
│   │   │   └── 00_create_schemas.sql (existing)
│   │   ├── 02_source/
│   │   │   └── source.globus_file_index.sql (existing)
│   │   ├── 03_processed/
│   │   │   ├── processed.batch_metadata.sql (NEW)
│   │   │   ├── processed.batch_stage_status.sql (existing)
│   │   │   ├── processed.developed_images.sql (NEW)
│   │   │   ├── processed.detections.sql (NEW)
│   │   │   ├── processed.segmentations.sql (NEW)
│   │   │   ├── processed.cutouts.sql (NEW)
│   │   │   └── processed.cutout_features.sql (NEW)
│   │   ├── 05_logs/
│   │   │   ├── logs.processing_events.sql (update existing)
│   │   │   └── logs.juno_transfers.sql (existing)
│   │   └── 06_report/
│   │       ├── report.pipeline_gaps.sql (update existing)
│   │       ├── report.missing_on_juno.sql (existing)
│   │       └── report.batch_pipeline_status.sql (NEW)
│   └── migrations/
│       └── 001_add_processing_tables.sql
│
├── scripts/
│   ├── apply_schemas.py            # Apply all SQL schemas
│   ├── process_batches.py          # Worker: raw_to_jpg
│   ├── transfer_batches.py         # Worker: JUNO transfers
│   └── migrate_from_sqlite.py      # Migration tool
│
├── tests/
│   ├── test_connection.py
│   ├── test_gaps.py
│   ├── test_stages.py
│   ├── test_images.py
│   ├── test_transfers.py
│   ├── test_events.py
│   └── test_integration.py
│
├── pyproject.toml
└── README.md
```