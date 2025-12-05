Here is a **very short, simple, command-focused README** for getting started with your Slurm-hosted PostgreSQL server.

---

# **README — Starting & Connecting to the AgIR PostgreSQL Server**

## **1. Start the Postgres server on SciNet**

Submit the Slurm job:

```bash
sbatch server/db_server.sh
```

Check the job status:

```bash
squeue -u $USER
```

View the log to get the node + port:

```bash
tail -f /project/dash_agir/logs/pg_server_*.out.log
```

The log will show something like:

```
PGHOST=ceres20-compute-45.ceres.scinet.usda.gov
PGPORT=50412
```

A convenience file is also created:

```
/project/dash_agir/postgres/pg_coords.env
```

Run it using:
```sh
source /project/dash_agir/postgres/pg_coords.env
```


---

## **2. Enable passwordless login**

Your `~/.pgpass` file must have the entry:

```
hostname:port:semif_agir:<yourusername>:<yourpassword>
```

The script updates this automatically each run.

---

## **3. Connect to the database**

**Option A — One-liner using env vars**

```bash
source /project/dash_agir/postgres/pg_coords.env
psql -h $PGHOST -p $PGPORT -d semif_agir -U $USER
```

**Option B — Without sourcing**

```bash
psql -h <PGHOST> -p <PGPORT> -d semif_agir -U $USER
```

---

## **4. Create an alias for quick access (recommended)**

Add to `~/.bashrc`:

```bash
alias pgsemif='source /project/dash_agir/postgres/pg_coords.env && psql'
```

Reload:

```bash
source ~/.bashrc
```

Now connect anytime with:

```bash
pgsemif
```

---

## **5. Stop the server**

The job stops automatically when it reaches its Slurm time limit.

Or stop manually:

```bash
scancel <jobid>
```
