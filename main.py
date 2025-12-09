from agir_db import AgirDB, StageAlreadyInProgressError
import logging
from svs_raw_api import RawToDng, DngToJpg, load_config
# Configure logging
from agir_db import setup_logging
setup_logging(level=logging.INFO)

def process_batch_workflow():
    """Complete workflow for processing a batch."""
    
    with AgirDB() as db:
        # Discover work
        batches = db.gaps.get_batches_with_gaps(
            stage='raw_to_jpg',
            limit=1
        )
        
        if not batches:
            print("No batches need processing")
            return
        
        batch = batches[0]
        batch_id = batch['batch_id']
        print(f"Processing {batch_id}: {batch['gap_count']} images need conversion")
        
        # try:
        #     # Start stage
        #     db.stages.start(batch_id, 'raw_to_jpg', job_id='worker-001')
            
        #     # Get images needing processing
        #     images = db.gaps.get_images_with_gaps(batch_id, 'raw_to_jpg')
            
        #     # Process each image
        #     for img in images:
        #         try:
        #             # Convert RAW→JPG
        #             output_path = convert_to_jpg(
        #                 img['input_path'],
        #                 img['expected_output_path']
        #             )
                    
        #             # Update metadata
        #             db.images.update(
        #                 img['image_id'],
        #                 jpg_path=output_path
        #             )
                    
        #             # Log success
        #             db.events.log(
        #                 event_type='image_converted',
        #                 batch_id=batch_id,
        #                 image_id=img['image_id'],
        #                 stage='raw_to_jpg',
        #                 severity='info',
        #                 message='Successfully converted to JPG'
        #             )
                    
        #         except Exception as e:
        #             # Log error but continue
        #             db.events.log(
        #                 event_type='conversion_error',
        #                 batch_id=batch_id,
        #                 image_id=img['image_id'],
        #                 stage='raw_to_jpg',
        #                 severity='error',
        #                 message=f'Conversion failed: {str(e)}'
        #             )
            
        #     # Mark complete
        #     db.stages.complete(
        #         batch_id,
        #         'raw_to_jpg',
        #         success=True,
        #         files_processed=len(images)
        #     )
            
        #     print(f"Completed {batch_id}: processed {len(images)} images")
            
        # except StageAlreadyInProgressError:
        #     print(f"Batch {batch_id} already being processed")

if __name__ == '__main__':
    process_batch_workflow()