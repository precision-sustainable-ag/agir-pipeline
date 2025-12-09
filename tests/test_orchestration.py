from pathlib import Path

# Import your converters
from svs_raw_api import RawToDng, DngToJpg
from agir_db import AgirDB

# with AgirDB() as db:
#     # Test conversion summary
#     summary = db.orchestration.get_conversion_summary()
#     print(f"Queue: {summary['batches_in_queue']}")
#     print(f"Active: {summary['batches_active']}")
    
#     # Test conversion queue
#     queue = db.orchestration.get_conversion_queue(limit=5)
#     print(f"Found {len(queue)} batches needing conversion")
#     for batch in queue:
#         print(batch)



def convert_batch(batch_id: str):
    """Convert batch using svs-raw-api."""
    
    with AgirDB() as db:
        # Start conversion
        info = db.orchestration.get_batch_progress(
            batch_id,
            # job_id='worker-001'
        )
        for key, value in info.items():
            print(f"{key}: {value}")
        # print(type(info))
        # db.commit()
        
        # # Initialize converters
        # raw_to_dng = RawToDng()
        # dng_to_jpg = DngToJpg()
        
        # files_processed = 0
        # files_failed = 0
        
        # for file in info['files']:
        #     try:
        #         raw_path = file['file_path']
        #         dng_path = f"/tmp/{file['image_id']}.dng"
        #         jpg_path = f"/data/jpg/{file['image_id']}.jpg"
                
        #         # Convert RAW -> DNG -> JPG
        #         raw_to_dng.convert(raw_path, dng_path)
        #         dng_to_jpg.convert(dng_path, jpg_path)
                
        #         files_processed += 1
                
        #     except Exception as e:
        #         print(f"Failed {file['file_name']}: {e}")
        #         files_failed += 1
        
        # # Complete
        # db.orchestration.complete_batch_conversion(
        #     batch_id,
        #     success=(files_failed == 0),
        #     files_processed=files_processed,
        #     files_failed=files_failed
        # )
        # db.commit()

# Test it
convert_batch('MD_2025-10-29')