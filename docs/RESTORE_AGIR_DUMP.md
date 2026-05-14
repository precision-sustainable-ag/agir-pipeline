# Restoring a PostgreSQL dump from SciNet (minimal)

This restores a SciNet PostgreSQL dump into a **user-owned local Postgres instance**.
If you already have Postgres running, **use a different port**.

---

## 1. Create Postgres tools env

```bash
conda create -n pgtools -y
conda activate pgtools
conda install -c conda-forge postgresql -y
```

---

## 2. Set paths and choose a port

Pick **any free, non-default port** (not `5432`, not someone else’s).

```bash
export PGHOME="$HOME/pg_local"
export PGDATA="$PGHOME/data"
export PGSOCK="$PGHOME/socket"

export PGPORT=54355   # change this if needed
```

---

## 3. Initialize Postgres (once)

```bash
mkdir -p "$PGDATA" "$PGSOCK"
chmod 700 "$PGHOME" "$PGDATA" "$PGSOCK"

initdb -D "$PGDATA"
```

---

## 4. Minimal local config

```bash
cat >> "$PGDATA/postgresql.conf" <<EOF
listen_addresses = '127.0.0.1'
port = ${PGPORT}
unix_socket_directories = '${PGSOCK}'
EOF
```

---

## 5. Start server

```bash
pg_ctl -D "$PGDATA" -l "$PGHOME/postgres.log" start
```

---

## 6. Restore the dump

```bash
pg_restore -h "$PGSOCK" -p "$PGPORT" -U agir_admin \
  --clean --if-exists \
  --no-owner \
  -d agir \
  agirV2.dump
```

---

## 7. Export DB connection variables

Write connection coordinates to a file that can be sourced by scripts or setup tools.

```bash
cat > pg_coords.env <<EOF
export PGHOST=${PGSOCK}
export PGPORT=${PGPORT}
export PGDATABASE=agir
export PGUSER=agir_admin
EOF
```

Load later with:

```bash
source pg_coords.env
```

---

**Notes**

* If the port is in use, pick a different `PGPORT` and restart Postgres.
* Uses a Unix socket (no TCP exposure).
* Assumes database `agir` and role `agir_admin` already exist.

Stop when done:

```bash
pg_ctl -D "$PGDATA" stop
```