# Installation Guide

[← Back to Index](index.md)

Complete setup instructions for AgirDB.

---

## Requirements

### System Requirements

- Python 3.8 or higher
- PostgreSQL 12 or higher
- 4GB RAM minimum
- Network access to PostgreSQL server

### Python Dependencies

Automatically installed with package:
- `psycopg2-binary` >= 2.9.0
- `pyyaml` >= 6.0

---

## Installation

### Via pip (Recommended)

```bash
pip install agir-db
```

### From Source

```bash
git clone https://github.com/yourusername/agir-db.git
cd agir-db
pip install -e .
```

### Development Install

```bash
git clone https://github.com/yourusername/agir-db.git
cd agir-db
pip install -e ".[dev]"
```

---

## PostgreSQL Setup

### Install PostgreSQL

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

**macOS:**
```bash
brew install postgresql@14
brew services start postgresql@14
```

**Windows:**
Download installer from [postgresql.org](https://www.postgresql.org/download/windows/)

---

### Create Database and User

```bash
# Connect as postgres user
sudo -u postgres psql

# In PostgreSQL prompt:
CREATE DATABASE agir;
CREATE USER agir_user WITH ENCRYPTED PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE agir TO agir_user;

# Exit
\q
```

---

### Initialize Schema

```bash
# Connect to database
psql -h localhost -U agir_user -d agir

# Run schema creation script (provided with package)
\i /path/to/agir_db/schema.sql
```

Or use the migration tool:
```python
from agir_db import AgirDB

db = AgirDB()
db.connect()
# Schema is auto-created on first connection
db.close()
```

---

## Configuration

### Environment Variables

**Recommended method:**

Create a `.env` file:
```bash
# .env
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agir
export PGUSER=agir_user
# Don't put password here - use .pgpass instead
```

Load environment:
```bash
source .env
```

---

### .pgpass File

**Recommended for password management:**

Create `~/.pgpass`:
```
# Format: hostname:port:database:username:password
localhost:5432:agir:agir_user:your_secure_password
```

Set permissions:
```bash
chmod 600 ~/.pgpass
```

**Why .pgpass?**
- More secure than environment variables
- Standard PostgreSQL authentication method
- Works across tools (psql, pgAdmin, AgirDB)

---

### Direct Credentials (Not Recommended)

Only for development:
```python
from agir_db import AgirDB

db = AgirDB(
    host='localhost',
    port=5432,
    dbname='agir',
    user='agir_user',
    password='your_secure_password'  # Don't do this in production!
)
```

---

## Verify Installation

### Test Connection

```python
from agir_db import AgirDB

try:
    with AgirDB() as db:
        print("✓ Connected successfully")
        print(f"Connection status: {db.is_connected}")
except Exception as e:
    print(f"✗ Connection failed: {e}")
```

### Run Test Suite

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run tests
pytest tests/

# With coverage
pytest --cov=agir_db tests/
```

---

## Initial Setup

### 1. Create Your First Batch

```python
from agir_db import AgirDB
from datetime import datetime

with AgirDB() as db:
    db.batches.insert(
        batch_id='TEST_BATCH',
        collection_date=datetime.now().strftime('%Y-%m-%d'),
        location='Test_Location',
        camera_id='TEST_CAM',
        image_count=0
    )
    print("✓ Test batch created")
```

### 2. Insert Sample Image

```python
with AgirDB() as db:
    db.images.insert(
        image_id='TEST_001',
        batch_id='TEST_BATCH',
        camera_id='TEST_CAM',
        capture_time=datetime.now().isoformat(),
        raw_path='/data/test/TEST_001.ARW',
        metadata={'test': True}
    )
    print("✓ Test image created")
```

### 3. Verify Gap Analysis

```python
with AgirDB() as db:
    batches = db.gaps.get_batches_with_gaps('raw_to_jpg')
    print(f"✓ Found {len(batches)} batches with gaps")
```

---

## Production Deployment

### PostgreSQL Configuration

Edit `postgresql.conf`:
```conf
# Connection Settings
max_connections = 100
shared_buffers = 256MB

# Performance
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB

# Logging
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d.log'
log_statement = 'ddl'
```

### Connection Pooling

For high-concurrency applications, use pgBouncer:

```bash
# Install
sudo apt install pgbouncer

# Configure /etc/pgbouncer/pgbouncer.ini
[databases]
agir = host=localhost port=5432 dbname=agir

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
pool_mode = transaction
max_client_conn = 100
default_pool_size = 20
```

Connect through pgBouncer:
```bash
export PGHOST=localhost
export PGPORT=6432  # pgBouncer port
```

---

## Docker Deployment

### Docker Compose

```yaml
# docker-compose.yml
version: '3.8'

services:
  postgres:
    image: postgres:14
    environment:
      POSTGRES_DB: agir
      POSTGRES_USER: agir_user
      POSTGRES_PASSWORD: secure_password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
  
  agir-worker:
    build: .
    environment:
      PGHOST: postgres
      PGPORT: 5432
      PGDATABASE: agir
      PGUSER: agir_user
    volumes:
      - ./data:/data
    depends_on:
      - postgres

volumes:
  postgres_data:
```

### Dockerfile

```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "worker.py"]
```

---

## Monitoring

### Enable Logging

```python
from agir_db import setup_logging
import logging

setup_logging(level=logging.INFO)
```

### Log to File

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agir_db.log'),
        logging.StreamHandler()
    ]
)
```

---

## Backup and Recovery

### Database Backups

```bash
# Daily backup
pg_dump -h localhost -U agir_user agir > backup_$(date +%Y%m%d).sql

# Compressed backup
pg_dump -h localhost -U agir_user agir | gzip > backup_$(date +%Y%m%d).sql.gz
```

### Automated Backups

```bash
# Add to crontab
0 2 * * * /usr/bin/pg_dump -h localhost -U agir_user agir | gzip > /backups/agir_$(date +\%Y\%m\%d).sql.gz
```

### Restore

```bash
# From uncompressed backup
psql -h localhost -U agir_user agir < backup_20250115.sql

# From compressed backup
gunzip -c backup_20250115.sql.gz | psql -h localhost -U agir_user agir
```

---

## Troubleshooting Installation

### psycopg2 Build Errors

**Error:** `pg_config executable not found`

**Solution:**
```bash
# Ubuntu/Debian
sudo apt install libpq-dev python3-dev

# macOS
brew install postgresql

# Or use binary wheel
pip install psycopg2-binary
```

---

### Permission Denied

**Error:** `permission denied for schema public`

**Solution:**
```sql
GRANT ALL PRIVILEGES ON SCHEMA public TO agir_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO agir_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO agir_user;
```

---

### Connection Refused

**Error:** `could not connect to server: Connection refused`

**Check:**
1. PostgreSQL is running
2. `postgresql.conf` allows connections
3. `pg_hba.conf` allows user authentication
4. Firewall allows port 5432

---

## Next Steps

After installation:
1. Read [Quick Start](README.md#quick-start)
2. Review [Orchestration Examples](orchestration.md)
3. Follow [Best Practices](best-practices.md)

---

[← Back to Index](index.md)
