# Phase 8: Analytics - Complete ✓

## Overview

Phase 8 implements comprehensive analytics and reporting capabilities for monitoring pipeline performance, throughput, errors, storage, and transfers. This provides the visibility needed for operational dashboards and performance optimization.

**Why Analytics?**
- **Monitor performance**: Real-time visibility into processing metrics
- **Identify bottlenecks**: Find slow stages and optimization opportunities  
- **Track errors**: Proactive error monitoring and trending
- **Storage planning**: Understand growth patterns and capacity needs
- **Transfer optimization**: Monitor transfer performance by route

## Components Created

### 1. **SQL Views** (analytics_schema.sql, ~480 lines)

**16 Pre-aggregated Views:**

1. `daily_batch_summary` - Daily processing volumes by state
2. `stage_performance` - Stage execution metrics with throughput
3. `stage_performance_summary` - Aggregate metrics by stage
4. `recent_errors` - Recent failures across all stages
5. `error_summary_by_stage` - Error statistics by stage
6. `transfer_performance` - Transfer metrics with throughput
7. `transfer_summary_by_route` - Statistics by source/destination
8. `batch_completion_status` - Comprehensive batch progress
9. `daily_throughput` - Daily processing throughput
10. `event_summary` - Event statistics by type/severity
11. `recent_critical_events` - Last 100 critical events
12. `storage_by_location` - Storage utilization by location
13. `storage_growth` - Monthly storage growth
14. `camera_usage_stats` - Statistics by camera
15. `pipeline_overview` - High-level pipeline status
16. Plus helper function: `get_processing_stats()`

### 2. **Analytics Class** (analytics.py, ~770 lines)

Comprehensive reporting methods:

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Pipeline overview
    overview = db.analytics.get_pipeline_overview()
    
    # Processing stats
    stats = db.analytics.get_processing_stats(days=7)
    
    # Throughput
    throughput = db.analytics.get_throughput(days=30)
    
    # Errors
    errors = db.analytics.get_error_summary(days=7)
    error_rate = db.analytics.get_error_rate('raw_to_jpg', days=30)
    
    # Storage
    storage = db.analytics.get_storage_by_location()
    growth = db.analytics.get_storage_growth(months=12)
    
    # Transfers
    perf = db.analytics.get_transfer_performance(days=7)
    summary = db.analytics.get_transfer_summary_by_route()
    
    # Batch details
    batch = db.analytics.get_batch_summary('MD_2025-01-01')
```

**Main Methods (14 total):**

1. **`get_pipeline_overview()`** - High-level status
2. **`get_processing_stats(days=30)`** - Processing metrics
3. **`get_daily_volumes(days=30, batch_state=None)`** - Daily volumes
4. **`get_throughput(days=30, stage=None)`** - Throughput metrics
5. **`get_stage_performance(stage=None)`** - Stage statistics
6. **`get_error_summary(stage=None, days=None)`** - Error summary
7. **`get_recent_errors(limit=50)`** - Recent error list
8. **`get_error_rate(stage=None, days=30)`** - Error rate calculation
9. **`get_transfer_performance(days=30, ...)`** - Transfer metrics
10. **`get_transfer_summary_by_route()`** - Route statistics
11. **`get_storage_by_location()`** - Storage by location
12. **`get_storage_growth(months=12, batch_state=None)`** - Growth trends
13. **`get_batch_summary(batch_id)`** - Comprehensive batch info
14. **`get_camera_stats()`** - Camera usage statistics


## Installation

### Step 1: Install SQL Views

```bash
# Connect to database
source /project/dash_agir/postgres/pg_coords.env
psql

# Run schema file
\i /path/to/analytics_schema.sql

# Verify views exist
\dv processed.*
```

Expected output shows 16+ views including:
- daily_batch_summary
- stage_performance
- transfer_performance
- storage_by_location
- pipeline_overview
- etc.

### Step 2: Update Python Package

```bash
cd /path/to/agir-db
pip install -e .
```

## Testing

```bash
python test_phase8.py
```

Expected output:
```
✓ All Phase 8 unit tests passed!
✓ All database integration tests passed!
✓ Phase 8 Complete!
```

## Usage Examples

### Example 1: Daily Operations Dashboard

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get high-level overview
    overview = db.analytics.get_pipeline_overview()
    
    print("="*60)
    print("DAILY OPERATIONS DASHBOARD")
    print("="*60)
    
    print(f"\n📊 Pipeline Status:")
    print(f"  Total batches: {overview['total_batches']}")
    print(f"  Completed: {overview['completed_batches']}")
    print(f"  In progress: {overview['in_progress_batches']}")
    print(f"  Failed: {overview['failed_batches']}")
    
    print(f"\n💾 Storage:")
    print(f"  Total: {overview['total_storage_gb']:.2f} GB")
    
    print(f"\n🔄 Active Operations:")
    print(f"  Running stages: {overview['running_stages']}")
    print(f"  Active transfers: {overview['active_transfers']}")
    print(f"  Pending transfers: {overview['pending_transfers']}")
    
    # Get today's processing
    stats = db.analytics.get_processing_stats(days=1)
    print(f"\n📈 Today's Processing:")
    print(f"  Batches: {stats.get('batches_processed', 0)}")
    print(f"  Files: {stats.get('files_processed', 0)}")
    print(f"  Data: {stats.get('total_gb_processed', 0):.2f} GB")
    
    # Check for errors
    errors = db.analytics.get_recent_errors(limit=5)
    if errors:
        print(f"\n⚠️  Recent Errors: {len(errors)}")
        for e in errors[:3]:
            print(f"  {e['batch_id']} {e['stage']}: {e['error_message'][:50]}")
```

### Example 2: Weekly Performance Report

```python
from agir_db import AgirDB
from datetime import date

with AgirDB() as db:
    print("WEEKLY PERFORMANCE REPORT")
    print("="*60)
    
    # Processing stats
    stats = db.analytics.get_processing_stats(days=7)
    print(f"\n📊 Processing (Last 7 Days):")
    print(f"  Batches processed: {stats.get('batches_processed', 0)}")
    print(f"  Files processed: {stats.get('files_processed', 0)}")
    print(f"  Total data: {stats.get('total_gb_processed', 0):.2f} GB")
    print(f"  Avg files/batch: {stats.get('avg_files_per_batch', 0):.1f}")
    
    # Stage performance
    print(f"\n⚡ Stage Performance:")
    performance = db.analytics.get_stage_performance()
    for p in performance:
        print(f"\n  {p['stage']}:")
        print(f"    Executions: {p['total_executions']}")
        print(f"    Success rate: {p.get('success_rate', 0):.1f}%")
        print(f"    Avg duration: {p.get('avg_duration_seconds', 0):.1f}s")
        print(f"    Throughput: {p.get('avg_files_per_second', 0):.2f} files/sec")
    
    # Error analysis
    print(f"\n⚠️  Error Analysis:")
    error_rate = db.analytics.get_error_rate(days=7)
    print(f"  Overall error rate: {error_rate['error_rate']:.2f}%")
    print(f"  Failed executions: {error_rate['failed_executions']}")
    
    errors_by_stage = db.analytics.get_error_summary(days=7)
    if errors_by_stage:
        print(f"\n  Errors by stage:")
        for e in errors_by_stage:
            print(f"    {e['stage']}: {e['error_count']} errors")
    
    # Transfer performance
    print(f"\n🔄 Transfer Performance:")
    route_summary = db.analytics.get_transfer_summary_by_route()
    for r in route_summary:
        route = f"{r['source_location']} → {r['destination_location']}"
        print(f"\n  {route}:")
        print(f"    Transfers: {r['total_transfers']}")
        print(f"    Data: {r.get('total_gb_transferred', 0):.2f} GB")
        print(f"    Avg rate: {r.get('avg_transfer_rate_mbps', 0):.1f} MB/s")
```

### Example 3: Throughput Analysis

```python
from agir_db import AgirDB
import pandas as pd
import matplotlib.pyplot as plt

with AgirDB() as db:
    # Get daily throughput
    throughput = db.analytics.get_throughput(days=30)
    
    # Convert to DataFrame
    df = pd.DataFrame(throughput)
    
    # Group by stage
    for stage in df['stage'].unique():
        stage_df = df[df['stage'] == stage]
        
        plt.figure(figsize=(12, 6))
        plt.plot(
            stage_df['processing_date'],
            stage_df['avg_files_per_second'],
            marker='o'
        )
        plt.title(f'Throughput: {stage}')
        plt.xlabel('Date')
        plt.ylabel('Files per Second')
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(f'throughput_{stage}.png')
        print(f"Saved throughput_{stage}.png")
```

### Example 4: Storage Planning

```python
from agir_db import AgirDB

with AgirDB() as db:
    print("STORAGE ANALYSIS")
    print("="*60)
    
    # Current storage
    print("\n💾 Current Storage by Location:")
    storage = db.analytics.get_storage_by_location()
    
    total_gb = 0
    for s in storage:
        print(f"\n  {s['location']}:")
        print(f"    Batches: {s['batch_count']}")
        print(f"    Files: {s['total_raw_files']:,} RAW, {s['total_jpg_files']:,} JPG")
        print(f"    Storage: {s['total_gb']:.2f} GB")
        print(f"    Avg/batch: {s.get('avg_gb_per_batch', 0):.2f} GB")
        print(f"    Date range: {s['earliest_batch']} to {s['latest_batch']}")
        total_gb += s['total_gb']
    
    print(f"\n  TOTAL: {total_gb:.2f} GB")
    
    # Growth trends
    print("\n📈 Storage Growth (Last 12 Months):")
    growth = db.analytics.get_storage_growth(months=12)
    
    for g in growth:
        print(f"  {g['month']}: {g['total_gb']:.2f} GB ({g['batch_count']} batches)")
    
    # Calculate growth rate
    if len(growth) >= 2:
        recent = growth[0]['total_gb']
        old = growth[-1]['total_gb']
        monthly_rate = (recent - old) / len(growth)
        print(f"\n  Avg growth: {monthly_rate:.2f} GB/month")
        
        # Project future storage (12 months)
        projected = total_gb + (monthly_rate * 12)
        print(f"  Projected (1 year): {projected:.2f} GB")
```

### Example 5: Error Investigation

```python
from agir_db import AgirDB

with AgirDB() as db:
    # Get recent errors
    print("ERROR INVESTIGATION")
    print("="*60)
    
    errors = db.analytics.get_recent_errors(limit=20)
    
    print(f"\nFound {len(errors)} recent errors\n")
    
    # Group by error type
    error_types = {}
    for e in errors:
        msg = e['error_message'][:50] if e['error_message'] else 'Unknown'
        error_types[msg] = error_types.get(msg, 0) + 1
    
    print("Most common errors:")
    for msg, count in sorted(error_types.items(), key=lambda x: x[1], reverse=True)[:5]:
        print(f"  [{count}x] {msg}")
    
    # Detailed error list
    print("\nRecent error details:")
    for e in errors[:10]:
        print(f"\n  Batch: {e['batch_id']}")
        print(f"  Stage: {e['stage']}")
        print(f"  Time: {e['updated_at']}")
        print(f"  Error: {e['error_message']}")
        print(f"  Files failed: {e['files_failed']}")
```


### Example 6: Batch Deep Dive

```python
from agir_db import AgirDB

def analyze_batch(batch_id):
    with AgirDB() as db:
        summary = db.analytics.get_batch_summary(batch_id)
        
        if not summary:
            print(f"Batch {batch_id} not found")
            return
        
        print(f"BATCH ANALYSIS: {batch_id}")
        print("="*60)
        
        print(f"\n📦 Batch Info:")
        print(f"  State: {summary['batch_state']}")
        print(f"  Date: {summary['batch_date']}")
        print(f"  Location: {summary['location']}")
        print(f"  Status: {summary['batch_status']}")
        
        print(f"\n📊 Files:")
        print(f"  RAW: {summary['file_count_raw']}")
        print(f"  JPG: {summary['file_count_jpg']}")
        
        print(f"\n⚙️  Stage Progress:")
        print(f"  Completed: {summary['stages_completed']}")
        print(f"  Failed: {summary['stages_failed']}")
        print(f"  Running: {summary['stages_running']}")
        
        print(f"\n🔄 Pipeline Status:")
        print(f"  RAW→JPG: {'✓' if summary['raw_to_jpg_complete'] else '✗'}")
        print(f"  JPG→Metadata: {'✓' if summary['jpg_to_metadata_complete'] else '✗'}")
        print(f"  Metadata→Cutouts: {'✓' if summary['metadata_to_cutouts_complete'] else '✗'}")
        
        print(f"\n📤 Transfers:")
        print(f"  Completed: {summary['transfers_completed']}")
        print(f"  Failed: {summary['transfers_failed']}")
        print(f"  In progress: {summary['transfers_in_progress']}")
        
        print(f"\n⏱️  Timing:")
        print(f"  First stage: {summary['first_stage_start']}")
        print(f"  Last stage: {summary['last_stage_end']}")
        print(f"  Created: {summary['created_at']}")
        print(f"  Updated: {summary['updated_at']}")

# Use it
analyze_batch('MD_2025-01-01')
```

## Integration with Previous Phases

Analytics leverages ALL previous phases:

### With Phase 2 (Pipeline Gaps)
Find work, then monitor performance:
```python
gaps = db.gaps.get_batches_with_gaps('raw_to_jpg')
throughput = db.analytics.get_throughput(stage='raw_to_jpg', days=7)
```

### With Phase 3 (Stage Status)
Monitor stage execution:
```python
performance = db.analytics.get_stage_performance('raw_to_jpg')
error_rate = db.analytics.get_error_rate('raw_to_jpg', days=30)
```

### With Phase 4 (Event Logging)
Analyze events:
```python
events = db.analytics.get_recent_errors()
# Events feed into error_summary_by_stage view
```

### With Phase 5 (Metadata)
Storage and camera analytics:
```python
storage = db.analytics.get_storage_by_location()
cameras = db.analytics.get_camera_stats()
```

### With Phase 6 (Inventory)
Processing volume tracking:
```python
db.inventory.sync_recent(days=7)
volumes = db.analytics.get_daily_volumes(days=7)
```

### With Phase 7 (Transfers)
Transfer performance:
```python
transfer_perf = db.analytics.get_transfer_performance(days=7)
route_summary = db.analytics.get_transfer_summary_by_route()
```

## View Descriptions

### Pipeline Overview
**`pipeline_overview`** - Single-row view with high-level counts:
- Batch counts (total, completed, failed, in-progress)
- Image counts  
- Active operations (stages, transfers)
- Storage totals
- Date ranges

### Daily Metrics
**`daily_batch_summary`** - Processing volumes by day:
- Batch counts by state
- File counts (RAW, JPG)
- Completion rates
- Average sizes

**`daily_throughput`** - Throughput by day and stage:
- Files processed
- Processing time
- Files per second
- Batches processed

### Stage Analytics
**`stage_performance`** - Individual stage executions:
- Duration, status
- Files processed/failed
- Calculated throughput
- Batch context

**`stage_performance_summary`** - Aggregate by stage:
- Execution counts
- Success rates
- Average durations
- Total throughput

### Error Analysis
**`recent_errors`** - Recent failures:
- Error messages
- Failed file counts
- Timestamps
- Batch context

**`error_summary_by_stage`** - Aggregated errors:
- Error counts by stage
- Affected batches
- Sample error messages
- Last occurrence

### Transfer Analytics
**`transfer_performance`** - Individual transfers:
- Duration, bytes, files
- Calculated throughput
- Status
- Route info

**`transfer_summary_by_route`** - Aggregate by route:
- Transfer counts
- Success rates
- Average speeds
- Total data moved

### Storage Analytics
**`storage_by_location`** - Current storage:
- File counts by type
- Total storage
- Batch counts
- Date ranges

**`storage_growth`** - Monthly trends:
- Growth by month
- Cumulative totals
- By batch state
- File type breakdown

### Batch Tracking
**`batch_completion_status`** - Comprehensive batch view:
- Stage completion counts
- Pipeline flags
- Transfer status
- Timing information

### Camera Analytics
**`camera_usage_stats`** - By camera model:
- Image counts
- Batch counts
- Detection statistics
- Date ranges

## API Reference

See analytics.py for detailed method signatures and parameters.

**Key Methods:**
- `get_pipeline_overview()` - High-level status
- `get_processing_stats(days)` - Processing metrics
- `get_throughput(days, stage)` - Throughput analysis
- `get_error_rate(stage, days)` - Error rate calculation
- `get_storage_by_location()` - Storage utilization
- `get_batch_summary(batch_id)` - Detailed batch info

## Files Created

```
agir-db/
├── sql/schemas/03_processed/
│   └── analytics_schema.sql             # 16 views + functions (480 lines)
│
├── src/agir_db/
│   ├── analytics.py                     # Analytics class (770 lines)
│   ├── api.py                           # Updated integration
│   └── __init__.py                      # Updated exports
│
└── tests/
    └── test_phase8.py                   # Test suite (330 lines)

Total new code: ~1,580 lines
```

## Status

**Phase 8: COMPLETE ✓**

All analytics components are implemented and tested:
- ✓ 16 SQL views for reporting
- ✓ Helper function for date range stats
- ✓ Analytics class (14 methods)
- ✓ Integration with AgirDB facade
- ✓ Test suite (unit + integration tests)
- ✓ Comprehensive documentation

**Ready for Phase 9 (Migration Tools)!**