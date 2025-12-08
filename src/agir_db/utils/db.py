# scripts/utils/db.py
from dataclasses import dataclass
import os
import psycopg2
import psycopg2.extras

@dataclass
class DBConfig:
    host: str
    port: int
    dbname: str
    user: str

    @classmethod
    def from_env(cls):
        return cls(
            host=os.environ.get("PGHOST"),
            port=int(os.environ.get("PGPORT")),
            dbname=os.environ.get("PGDATABASE", "agir"),
            user=os.environ.get("PGUSER", "matthew.kutugata"),
        )

def get_conn():
    cfg = DBConfig.from_env()
    conn = psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        # password comes from .pgpass on SciNet
    )
    return conn
