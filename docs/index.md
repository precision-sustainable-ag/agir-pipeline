---
layout: default
title: Home
nav_order: 1
---


# AgirDB API Documentation

# AgirDB API Documentation

**Version:** 1.0.0 | **Author:** Matthew Kutugata

## Overview

AgirDB is a PostgreSQL-backed API for managing agricultural image processing pipelines. It uses "pipeline gaps" methodology (missing output files) as the source of truth for work discovery, providing robust and self-correcting workflow orchestration.

### Quick Links

- [Installation & Setup](#installation--setup)
- [Quick Start](#quick-start)
- [Complete Workflows](orchestration.md)
- [Best Practices](best-practices.md)
- [Exception Handling](exceptions.md)
- [Troubleshooting](troubleshooting.md)
- [Database Schema](schema.md)

---

## Installation & Setup

```bash
pip install agir-db
```

### Configuration

```python
from agir_db import AgirDB

# Use environment variables (recommended)
with AgirDB() as db:
    # Your code here
    pass

# Or pass credentials directly
db = AgirDB(host='localhost', port=5432, dbname='agir', user='agir_user')
```

See [Installation Guide](installation.md) for detailed setup instructions.

---

## Quick Start

```python
from agir_db import AgirDB

# Discover batches needing processing
with AgirDB() as db:
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg', limit=10)
    
    for batch in batches:
        # Start processing
        db.stages.start(batch['batch_id'], 'raw_to_jpg')
        
        # Process images
        images = process_images(batch)
        db.images.insert_bulk(images)
        
        # Mark complete
        db.stages.complete(batch['batch_id'], 'raw_to_jpg', success=True)
```

See [Orchestration Guide](orchestration.md) for complete workflow examples.

---

## API Reference

### Core Connection Methods
[View Details](core-connection.md)

| Method | Description |
|--------|-------------|
| `AgirDB.__init__()` | Initialize database connection with credentials |
| `connect()` | Establish database connection |
| `close()` | Close database connection |
| `commit()` | Commit current transaction |
| `rollback()` | Rollback current transaction |
| `is_connected` | Check if connection is active (property) |

---

### Pipeline Gaps Component (`db.gaps`)
[View Details](pipeline-gaps.md)

| Method | Description |
|--------|-------------|
| `get_batches_with_gaps()` | Get batches with missing output files for a stage |
| `get_images_with_gaps()` | Get specific images missing output files for a batch and stage |
| `get_gap_summary()` | Get summary statistics of gaps across all batches |
| `check_batch_complete()` | Check if batch has all expected outputs for a stage |
| `get_stage_progress()` | Get processing progress for a batch and stage |

---

### Stage Status Component (`db.stages`)
[View Details](stage-status.md)

| Method | Description |
|--------|-------------|
| `start()` | Mark stage as started for a batch (prevents duplicate work) |
| `complete()` | Mark stage as completed (success or failure) |
| `get_status()` | Get current status of a stage for a batch |
| `get_in_progress()` | Get all currently in-progress stages |
| `cancel()` | Cancel an in-progress stage |
| `get_history()` | Get processing history for a batch across all stages |

---

### Image Metadata Component (`db.images`)
[View Details](image-metadata.md)

| Method | Description |
|--------|-------------|
| `insert()` | Insert metadata for a single image |
| `insert_bulk()` | Insert metadata for multiple images efficiently |
| `get()` | Get metadata for a specific image |
| `update()` | Update metadata for an existing image |
| `get_by_batch()` | Get all images for a specific batch |
| `search()` | Search images by various criteria |
| `delete()` | Delete an image record (does not delete files) |

---

### Batch Metadata Component (`db.batches`)
[View Details](batch-metadata.md)

| Method | Description |
|--------|-------------|
| `insert()` | Insert metadata for a new batch |
| `get()` | Get metadata for a specific batch |
| `update()` | Update metadata for an existing batch |
| `list()` | List all batches with optional filtering |
| `delete()` | Delete a batch record (does not delete files) |

---

### Event Logging Component (`db.events`)
[View Details](event-logging.md)

| Method | Description |
|--------|-------------|
| `log()` | Log a single processing event |
| `log_bulk()` | Log multiple events efficiently |
| `get()` | Get a specific event by ID |
| `search()` | Search events by various criteria |
| `get_recent()` | Get most recent events |

---

### Inventory Synchronization Component (`db.inventory`)
[View Details](inventory-sync.md)

| Method | Description |
|--------|-------------|
| `scan_directory()` | Scan directory and sync file inventory with database |
| `verify_batch()` | Verify file existence for all images in a batch |
| `mark_missing()` | Mark files as missing in database |
| `get_missing()` | Get list of images with missing files |

---

### Transfer Management Component (`db.transfers`)
[View Details](transfer-management.md)

| Method | Description |
|--------|-------------|
| `create()` | Create a new transfer request |
| `start()` | Mark transfer as started |
| `complete()` | Mark transfer as completed |
| `get_status()` | Get status of a transfer |
| `list_pending()` | Get all pending transfers |
| `cancel()` | Cancel a pending or in-progress transfer |

---

### Analytics Component (`db.analytics`)
[View Details](analytics.md)

| Method | Description |
|--------|-------------|
| `get_pipeline_summary()` | Get summary statistics for entire pipeline |
| `get_batch_statistics()` | Get detailed statistics for a specific batch |
| `get_processing_rates()` | Get processing rates and throughput metrics |
| `get_error_summary()` | Get summary of errors and failures |
| `export_report()` | Export analytics data to CSV or JSON |

---

### Migration Component (`db.migration`)
[View Details](migration.md)

| Method | Description |
|--------|-------------|
| `import_from_sqlite()` | Import all data from a SQLite database |
| `validate_sqlite()` | Validate SQLite database structure and data |

---

## Additional Resources

### Guides
- [Complete Workflows & Orchestration](orchestration.md) - End-to-end pipeline examples
- [Best Practices](best-practices.md) - Patterns for production use
- [Exception Handling](exceptions.md) - Error handling reference
- [Troubleshooting](troubleshooting.md) - Common issues and solutions

### Reference
- [Database Schema](schema.md) - Table structures and relationships
- [Performance Considerations](performance.md) - Optimization tips

---

## Key Concepts

### Pipeline Gaps as Source of Truth
AgirDB discovers work by identifying missing output files rather than relying solely on status tracking. This approach is self-correcting and handles edge cases like partial failures and interrupted processing gracefully.

### Generic Pipeline Stages
Stages use underscore naming conventions (e.g., `raw_to_dng`, `object_detection`) rather than hardcoded workflows, making the system extensible for future computer vision pipelines.

### Clean Separation
Clear boundaries between conversion logic and database operations enable easy integration into larger processing systems.

---

## Architecture

```
AgirDB (Main API)
├── gaps          # Pipeline gap analysis (work discovery)
├── stages        # Stage status tracking (in-progress monitoring)
├── images        # Image metadata management
├── batches       # Batch metadata management
├── transfers     # JUNO transfer operations
├── events        # Processing event logging
├── inventory     # File inventory synchronization
├── analytics     # Reporting and statistics
└── migration     # SQLite data import
```

---

## Support

- **Documentation**: This documentation set
- **Issues**: Report on GitHub repository
- **Contact**: Matthew Kutugata

---

## License

Copyright © 2025 Matthew Kutugata. All rights reserved.
