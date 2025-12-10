import pandas as pd
from agir_db import AgirDB

# Discover batches needing processing
with AgirDB() as db:
    # batches = db.gaps.get_batches_with_gaps(stage='raw_to_jpg')
    # print(f"Discovered {len(batches)} batches with gaps for stage 'raw_to_jpg'")
    # export to csv
    # df = pd.DataFrame(batches)
    # df.to_csv('batches_with_gaps.csv', index=False)
    # print(df.head())
    batches = db.transfers.get_batches_needing_juno_transfer(source_location="NCSU", data_state="developed_jpg")
    print(f"Discovered {len(batches)} batches needing Juno transfer from NCSU with data state 'developed_jpg'")
    pd.DataFrame(batches).to_csv("batches_needing_juno_transfer.csv", index=False)
    batch_id = batches[0]["batch_id"]
    print(batch_id)
    files = db.transfers.get_files_needing_juno_transfer(source_location="NCSU", data_state="developed_jpg", batch_id=batch_id)
    print(f"Discovered {len(files)} files needing Juno transfer from NCSU with data state 'developed_jpg'")
    pd.DataFrame(files).to_csv("files_needing_juno_transfer.csv", index=False)