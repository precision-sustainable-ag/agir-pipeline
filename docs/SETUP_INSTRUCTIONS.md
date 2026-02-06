# AGIR Pipeline Setup Script (uv)

This repo includes a single `setup.sh` script that bootstraps a **Python 3.12** `uv` virtual environment, installs the package in **editable** mode, optionally applies **PostgreSQL schemas**, and can run a small test entrypoint.

It supports both **local** machines and **HPC/SciNet-like** environments by auto-detecting (or forcing) the mode and placing the venv + uv cache in the appropriate filesystem.

---

## What it does

When you run `./setup.sh` (normal path), it will:

1. **Detect mode** (`local` vs `hpc`) or use `AGIR_MODE` / CLI flags.
2. **Ensure `uv` exists** (installs it to `~/.local/bin/uv` without sudo if missing).
3. **Ensure Python 3.12 exists**

   * Uses `python3.12` if present
   * Otherwise installs Python via `uv python install 3.12`
4. **Create or reuse a venv** at a mode-specific location.
5. **Install this repo in editable mode** (`uv pip install -e .`), optionally with extras.
6. **Verify imports** (`from agir_db import AgirDB`)
7. If a DB is reachable: **apply schemas** and optionally **run tests**.

---

## Quickstart

### Local setup (default: auto-detect → local)

```bash
./setup.sh --dev
# or
./setup.sh --all
```

### Force HPC mode (SciNet-style)

```bash
./setup.sh --hpc --dev
```

### Force local mode explicitly

```bash
./setup.sh --local --all
```

---

## Options

| Option          | Meaning                                       |
| --------------- | --------------------------------------------- |
| `--dev`         | Install editable with dev extras: `-e .[dev]` |
| `--all`         | Install editable with all extras: `-e .[all]` |
| `--hpc`         | Force HPC mode                                |
| `--local`       | Force local mode                              |
| `--schema-only` | Only apply DB schemas (no venv / install)     |
| `--test-only`   | Only run tests (assumes env already active)   |
| `--help`        | Print usage                                   |

---

## Environment variables

### Mode + Python + env recreation

| Env var             |         Default | Notes                              |
| ------------------- | --------------: | ---------------------------------- |
| `AGIR_MODE`         |          `auto` | `auto\|hpc\|local`                 |
| `PYTHON_VERSION`    |          `3.12` | Used for venv creation / checks    |
| `ENV_NAME`          | `agir_pipeline` | Venv folder name                   |
| `AGIR_RECREATE_ENV` |             `0` | If `1`, deletes venv and recreates |

**Recreate venv (non-interactive):**

```bash
AGIR_RECREATE_ENV=1 ./setup.sh --dev
```

### Database variables (for schema + tests)

The script considers the DB “reachable” if:

* `psql` is available, and
* `PGHOST` is set, and
* `psql -c "SELECT 1;"` succeeds.

Supported env vars:

* `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`

It also tries to source connection coordinates from:

* `/project/dash_agir/postgres/pg_coords.env` (HPC mode)
* `./pg_coords.env` (repo-local fallback)

---

## Paths and mode behavior

### Local mode

* **Venv:** `/home/$USER/software/uv/venvs/$ENV_NAME`
* **uv cache:** `/home/$USER/uv-cache`

### HPC mode

* **Venv:** `/project/dash_agir/$USER/software/uv/venvs/$ENV_NAME`
* **uv cache:** `/project/dash_agir/$USER/uv-cache`

Mode detection (`AGIR_MODE=auto`) is based on things like:

* presence of `/project/dash_agir`, or
* a module system + `/project` directory.

---

## Schema application

`--schema-only` (or the normal path when DB is reachable) applies SQL files under:

* `schemas/sql/source.globus_file_index.sql`
* `schemas/sql/logs.transfer_requests.sql` (best-effort)
* `schemas/sql/logs.transfer_runs.sql` (best-effort if file exists)
* `schemas/views/report.missing_on_juno.sql`

If the DB is not reachable during a normal run, schema application is skipped with warnings (and you can rerun later with `--schema-only`).

---

## Tests

`--test-only` runs:

* `tests/test_p1.py` (if it exists)

In the normal path, tests run only when the DB is reachable.

---

## After setup

Activate your environment manually (the script activates internally, but your shell won’t stay activated after it exits):

```bash
source /path/to/venv/bin/activate
```

Sanity check:

```bash
python -c "from agir_db import AgirDB; db=AgirDB(); db.connect(); print(db.is_connected)"
```

---

## Common workflows

### Install + schemas + tests (when DB is up)

```bash
./setup.sh --dev
```

### Install now, apply schemas later after DB starts

```bash
./setup.sh --dev
./setup.sh --schema-only
```

### Only run tests (env already activated)

```bash
source /path/to/venv/bin/activate
./setup.sh --test-only
```

---

## Notes / assumptions

* Requires `bash`, `curl` (only if `uv` must be installed), and optionally `psql` for schema/test steps.
* No sudo required. `uv` installs to `~/.local/bin`.
* The script asserts the venv Python is **3.12+** after activation.
* Requires a running postgresql server and associated pg_coords.env file in the repo root.