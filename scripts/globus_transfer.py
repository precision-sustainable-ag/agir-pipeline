from pathlib import Path
from agir_db import AgirDB, setup_logging
from agir_db.utils import db
import time

log = setup_logging()

# small function that writes a globus transfer batch file for specific data_state
def write_globus_transfer_batch_file(data_state: str,copy_domain: str, file_path: str):
    with AgirDB() as db:
        batches = db.transfers.get_batches_to_transfer(
            copy_domain=copy_domain,
            data_state=data_state,
            pick_policy="most_files",
        )

        with open(file_path, "w") as f:
            for batch in batches:
                storage_root = batch["storage_root"]
                batch_id = batch['batch_id']
                src_path = Path(storage_root) / data_state / batch_id
                dst_path = Path(f"/LTS/project/dash_agir/{data_state}/{batch_id}")
                
                if "developed" in data_state:
                    src_path = src_path / batch["parent_dir"]
                    dst_path = dst_path / batch["parent_dir"]

                # write source and destination paths to file
                f.write(f"{str(src_path)} {str(dst_path)} --recursive\n")

# Define endpoints
juno_endpoint = "904c2108-90cf-11e8-9672-0a6d4e044368"
ncsu_endpoint = "2f7f6170-8d5c-11e9-8e6a-029d279f7e24"

# Discover batches needing processing
data_options = {
    "semifield-upload": [
        "upload_raw"
    ],
    "semifield-developed-images": [
        "developed_images_jpg",
        "developed_metadata_json"
    ]
}

file_paths = []
script_root = Path("/project/dash_agir/matthew.kutugata/repos/agir-db/scripts/")
for data_state, copy_domains in data_options.items():
    for copy_domain in copy_domains:
        # add a timestamp to the file path to avoid overwriting
        timestamp = int(time.time())
        file_path = f"{script_root}/globus_transfer_{data_state}_{copy_domain}_{timestamp}.txt"
        
        write_globus_transfer_batch_file(
            data_state=data_state,
            copy_domain=copy_domain,
            file_path=file_path
        )
        file_paths.append(file_path)

        log.info(f"Globus transfer batch file written to: {file_path}")

        cmd = ["globus", "transfer",
            ncsu_endpoint,
            juno_endpoint,
            "--batch", file_path,
            "--label", f"NCSU_to_JUNO_{data_state}_{copy_domain}_batch_transfer"
        ]
        with AgirDB() as db:
            db.transfers.execute_transfer(
                cmd=cmd,
                dry_run=False
            )