# Workflow Orchestration

[← Back to Index](README.md)

Complete end-to-end workflow examples for common AgirDB usage patterns.

---

## Example 1: Basic RAW→JPG Processing Pipeline

Complete workflow for processing a batch through conversion.

```python
from agir_db import AgirDB, StageAlreadyInProgressError
import logging

# Configure logging
from agir_db import setup_logging
setup_logging(level=logging.INFO)

def process_batch_workflow():
    """Complete workflow for processing a batch."""
    
    with AgirDB() as db:
        # Discover work
        batches = db.gaps.get_batches_with_gaps(
            stage='raw_to_jpg',
            limit=1,
            order_by='gap_count',
            order_dir='DESC'
        )
        
        if not batches:
            print("No batches need processing")
            return
        
        batch = batches[0]
        batch_id = batch['batch_id']
        print(f"Processing {batch_id}: {batch['gap_count']} images need conversion")
        
        try:
            # Start stage
            db.stages.start(batch_id, 'raw_to_jpg', job_id='worker-001')
            
            # Get images needing processing
            images = db.gaps.get_images_with_gaps(batch_id, 'raw_to_jpg')
            
            # Process each image
            for img in images:
                try:
                    # Convert RAW→JPG
                    output_path = convert_to_jpg(
                        img['input_path'],
                        img['expected_output_path']
                    )
                    
                    # Update metadata
                    db.images.update(
                        img['image_id'],
                        jpg_path=output_path
                    )
                    
                    # Log success
                    db.events.log(
                        event_type='image_converted',
                        batch_id=batch_id,
                        image_id=img['image_id'],
                        stage='raw_to_jpg',
                        severity='info',
                        message='Successfully converted to JPG'
                    )
                    
                except Exception as e:
                    # Log error but continue
                    db.events.log(
                        event_type='conversion_error',
                        batch_id=batch_id,
                        image_id=img['image_id'],
                        stage='raw_to_jpg',
                        severity='error',
                        message=f'Conversion failed: {str(e)}'
                    )
            
            # Mark complete
            db.stages.complete(
                batch_id,
                'raw_to_jpg',
                success=True,
                files_processed=len(images)
            )
            
            print(f"Completed {batch_id}: processed {len(images)} images")
            
        except StageAlreadyInProgressError:
            print(f"Batch {batch_id} already being processed")

if __name__ == '__main__':
    process_batch_workflow()
```

---

## Example 2: Multi-Stage Pipeline

Process through RAW→DNG→JPG pipeline.

```python
from agir_db import AgirDB

def multi_stage_pipeline():
    """Process through RAW→DNG→JPG pipeline."""
    
    stages = ['raw_to_dng', 'dng_to_jpg']
    
    with AgirDB() as db:
        for stage in stages:
            print(f"\n=== Processing stage: {stage} ===")
            
            # Get pipeline health
            summary = db.gaps.get_gap_summary(stage)
            print(f"Overall completion: {100 - summary['overall_gap_percentage']:.1f}%")
            print(f"Batches with gaps: {summary['batches_with_gaps']}")
            
            # Process batches with gaps
            batches = db.gaps.get_batches_with_gaps(stage, limit=10)
            
            for batch in batches:
                batch_id = batch['batch_id']
                
                # Check if already in progress
                status = db.stages.get_status(batch_id, stage)
                if status and status['status'] == 'in_progress':
                    print(f"Skipping {batch_id} - already in progress")
                    continue
                
                # Process batch
                print(f"Processing {batch_id}: {batch['gap_count']} gaps")
                
                db.stages.start(batch_id, stage)
                
                # Get specific images needing work
                images = db.gaps.get_images_with_gaps(batch_id, stage)
                
                # Process images...
                process_images(images, stage)
                
                # Verify completion
                if db.gaps.check_batch_complete(batch_id, stage):
                    db.stages.complete(batch_id, stage, success=True)
                    print(f"✓ {batch_id} complete")
                else:
                    db.stages.complete(batch_id, stage, success=False,
                                     error_message='Some files still missing')
                    print(f"✗ {batch_id} incomplete")

if __name__ == '__main__':
    multi_stage_pipeline()
```

---

## Example 3: Monitoring and Analytics

Generate daily processing report.

```python
from agir_db import AgirDB
import datetime

def generate_daily_report():
    """Generate daily processing report."""
    
    with AgirDB() as db:
        # Get pipeline summary
        summary = db.analytics.get_pipeline_summary()
        
        print("=== Daily Pipeline Report ===\n")
        print(f"Total batches: {summary['total_batches']}")
        print(f"Total images: {summary['total_images']}\n")
        
        # Stage-by-stage summary
        for stage, stats in summary['stages'].items():
            print(f"{stage}:")
            print(f"  Complete: {stats['complete']}")
            print(f"  In Progress: {stats['in_progress']}")
            print(f"  Completion Rate: {stats['completion_rate']:.1f}%\n")
        
        # Processing rates (last 24 hours)
        yesterday = (datetime.datetime.now() - 
                    datetime.timedelta(days=1)).isoformat()
        
        for stage in ['raw_to_dng', 'dng_to_jpg']:
            rates = db.analytics.get_processing_rates(
                stage=stage,
                start_time=yesterday
            )
            
            print(f"\n{stage} throughput (24h):")
            print(f"  Images processed: {rates['total_images_processed']}")
            print(f"  Avg images/hour: {rates['avg_images_per_hour']:.1f}")
            print(f"  Avg time/image: {rates['avg_image_processing_time']:.2f}s")
        
        # Error summary
        errors = db.analytics.get_error_summary(start_time=yesterday)
        
        if errors['total_errors'] > 0:
            print(f"\n⚠ Errors in last 24h: {errors['total_errors']}")
            for error_type, count in errors['by_type'].items():
                print(f"  {error_type}: {count}")
        
        # Export detailed report
        report_path = db.analytics.export_report(
            report_type='pipeline_summary',
            format='csv',
            output_path=f'/reports/daily_{datetime.date.today()}.csv',
            start_time=yesterday
        )
        
        print(f"\nDetailed report saved to: {report_path}")

if __name__ == '__main__':
    generate_daily_report()
```

---

## Example 4: Batch Initialization

Initialize a new batch from filesystem.

```python
from agir_db import AgirDB
import os
from datetime import datetime

def initialize_new_batch(batch_directory: str):
    """Initialize a new batch from filesystem."""
    
    batch_id = os.path.basename(batch_directory)
    
    with AgirDB() as db:
        # Create batch record
        db.batches.insert(
            batch_id=batch_id,
            collection_date=datetime.now().strftime('%Y-%m-%d'),
            location='Field_North',
            camera_id='SVS_001',
            image_count=0,  # Will update after scan
            metadata={
                'source_directory': batch_directory,
                'initialized_by': 'script',
                'initialized_at': datetime.now().isoformat()
            }
        )
        
        # Scan RAW files
        result = db.inventory.scan_directory(
            directory=batch_directory,
            batch_id=batch_id,
            file_type='raw',
            pattern='*.ARW'
        )
        
        print(f"Initialized batch {batch_id}:")
        print(f"  Files found: {result['files_found']}")
        print(f"  Files added: {result['files_added']}")
        
        # Update batch with actual count
        db.batches.update(
            batch_id=batch_id,
            image_count=result['files_found']
        )
        
        # Log initialization
        db.events.log(
            event_type='batch_initialized',
            batch_id=batch_id,
            severity='info',
            message=f'Initialized with {result["files_found"]} images',
            metadata=result
        )
        
        return batch_id

if __name__ == '__main__':
    batch_id = initialize_new_batch('/data/raw/B123')
    print(f"Batch {batch_id} ready for processing")
```

---

## Example 5: Error Recovery

Find and recover stages stuck in 'in_progress' state.

```python
from agir_db import AgirDB
import datetime

def recover_stuck_stages():
    """Find and recover stages stuck in 'in_progress' state."""
    
    with AgirDB() as db:
        # Find stages in progress for more than 2 hours
        in_progress = db.stages.get_in_progress()
        timeout_threshold = 7200  # 2 hours in seconds
        
        for stage in in_progress:
            if stage['duration_seconds'] > timeout_threshold:
                batch_id = stage['batch_id']
                stage_name = stage['stage']
                
                print(f"Found stuck stage: {batch_id}/{stage_name}")
                print(f"  Duration: {stage['duration_seconds']}s")
                print(f"  Job ID: {stage['job_id']}")
                
                # Cancel the stuck stage
                db.stages.cancel(
                    batch_id,
                    stage_name,
                    reason=f'Timeout - exceeded {timeout_threshold}s limit'
                )
                
                # Log the recovery
                db.events.log(
                    event_type='stage_recovered',
                    batch_id=batch_id,
                    stage=stage_name,
                    severity='warning',
                    message='Stage cancelled due to timeout',
                    metadata={
                        'duration': stage['duration_seconds'],
                        'original_job_id': stage['job_id']
                    }
                )
                
                # Check if any files were actually processed
                progress = db.gaps.get_stage_progress(batch_id, stage_name)
                
                if progress['completion_percentage'] > 0:
                    print(f"  Partial completion: {progress['completion_percentage']:.1f}%")
                    print(f"  Remaining: {progress['remaining_images']} images")
                else:
                    print("  No progress made - full reprocessing needed")

if __name__ == '__main__':
    recover_stuck_stages()
```

---

## Example 6: Parallel Processing

Process multiple batches in parallel with multiple workers.

```python
from agir_db import AgirDB, StageAlreadyInProgressError
from concurrent.futures import ThreadPoolExecutor
import socket

def worker_process_batch(batch_id: str, stage: str, worker_id: str):
    """Worker function to process a single batch."""
    
    # Each worker gets its own connection
    with AgirDB() as db:
        try:
            # Try to claim the batch
            db.stages.start(
                batch_id,
                stage,
                job_id=worker_id,
                hostname=socket.gethostname()
            )
        except StageAlreadyInProgressError:
            print(f"[{worker_id}] Batch {batch_id} already claimed")
            return None
        
        try:
            # Process batch
            images = db.gaps.get_images_with_gaps(batch_id, stage)
            
            for img in images:
                process_image(img)
                db.images.update(img['image_id'], jpg_path=output_path)
            
            # Complete
            db.stages.complete(
                batch_id,
                stage,
                success=True,
                files_processed=len(images)
            )
            
            print(f"[{worker_id}] Completed {batch_id}: {len(images)} images")
            return batch_id
            
        except Exception as e:
            db.stages.complete(
                batch_id,
                stage,
                success=False,
                error_message=str(e)
            )
            print(f"[{worker_id}] Failed {batch_id}: {e}")
            return None

def parallel_processing():
    """Process batches in parallel with multiple workers."""
    
    num_workers = 4
    stage = 'raw_to_jpg'
    
    # Get work
    with AgirDB() as db:
        batches = db.gaps.get_batches_with_gaps(stage, limit=20)
        batch_ids = [b['batch_id'] for b in batches]
    
    print(f"Processing {len(batch_ids)} batches with {num_workers} workers")
    
    # Process in parallel
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        results = executor.map(
            lambda bid: worker_process_batch(bid, stage, f'worker-{executor._counter}'),
            batch_ids
        )
        
        completed = [r for r in results if r is not None]
        print(f"Completed {len(completed)}/{len(batch_ids)} batches")

if __name__ == '__main__':
    parallel_processing()
```

---

## Example 7: Integration with svs-raw-api

Integrate AgirDB with svs-raw-api for RAW processing.

```python
from agir_db import AgirDB
from svs_raw_api import RawToDng, DngToJpg
import yaml

def process_with_svs_raw_api():
    """Process batches using svs-raw-api converters."""
    
    # Load configuration
    with open('config.yaml') as f:
        config = yaml.safe_load(f)
    
    # Initialize converters
    raw_to_dng = RawToDng(config['raw_to_dng'])
    dng_to_jpg = DngToJpg(config['dng_to_jpg'])
    
    with AgirDB() as db:
        # Process RAW→DNG
        batches = db.gaps.get_batches_with_gaps('raw_to_dng', limit=5)
        
        for batch in batches:
            batch_id = batch['batch_id']
            db.stages.start(batch_id, 'raw_to_dng')
            
            images = db.gaps.get_images_with_gaps(batch_id, 'raw_to_dng')
            
            for img in images:
                # Convert using svs-raw-api
                output_path = raw_to_dng.convert(
                    img['input_path'],
                    img['expected_output_path']
                )
                
                # Update database
                db.images.update(img['image_id'], dng_path=output_path)
            
            if db.gaps.check_batch_complete(batch_id, 'raw_to_dng'):
                db.stages.complete(batch_id, 'raw_to_dng', success=True)
        
        # Process DNG→JPG
        batches = db.gaps.get_batches_with_gaps('dng_to_jpg', limit=5)
        
        for batch in batches:
            batch_id = batch['batch_id']
            db.stages.start(batch_id, 'dng_to_jpg')
            
            images = db.gaps.get_images_with_gaps(batch_id, 'dng_to_jpg')
            
            for img in images:
                # Convert using svs-raw-api
                output_path = dng_to_jpg.convert(
                    img['input_path'],
                    img['expected_output_path']
                )
                
                # Update database
                db.images.update(img['image_id'], jpg_path=output_path)
            
            if db.gaps.check_batch_complete(batch_id, 'dng_to_jpg'):
                db.stages.complete(batch_id, 'dng_to_jpg', success=True)

if __name__ == '__main__':
    process_with_svs_raw_api()
```

---

## See Also

- [Pipeline Gaps](pipeline-gaps.md) - Work discovery
- [Stage Status](stage-status.md) - Status tracking
- [Best Practices](best-practices.md) - Production patterns
- [Exception Handling](exceptions.md) - Error handling

[← Back to Index](README.md)
