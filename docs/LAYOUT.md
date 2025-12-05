```sh
agir-db/
│
├── server/
│   └── db_server.sh                     # Slurm job for Postgres (semif_agir)
│
├── ingest/                              # **source layer writers**
│   ├── globus_index_pg.py               # writes to source.globus_file_index
│   └── cli_globus_index.sh              # (optional) Slurm wrapper
│
├── status/                              # **processing status helpers**
│   ├── update_batch_status.py           # sets batch status in processed/status table
│   ├── update_image_status.py           # optional per-image status
│   └── cli_update_batch_status.sh       # simple CLI wrapper
│
├── etl/                                 # **source → processed → release**
│   ├── build_processed_semifield.py     # transforms globus_file_index → processed.semifield_images
│   ├── create_release_semifield_v1.py   # processed → release.semifield_images_v1
│   └── helpers.py
│
├── helpers/
│   ├── pg_connect.py                    # reads pg_coords.env, returns psycopg conn
│   ├── schema_init.py                   # creates tables in source/processed/release
│   └── utils.py
│
├── sql/
│   ├── create_schemas.sql               # source, processed, release
│   ├── create_source_tables.sql         # globus_file_index, etc.
│   ├── create_processed_tables.sql
│   └── create_release_templates.sql
│
└── README.md
```