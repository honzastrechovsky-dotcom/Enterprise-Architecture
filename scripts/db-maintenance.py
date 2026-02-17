#!/usr/bin/env python3
"""
Enterprise Agent Platform - Database Maintenance Script

Performs routine database maintenance:
  - VACUUM ANALYZE on all user tables
  - Reindexes bloated indexes (dead-tuple ratio above threshold)
  - Updates planner statistics (ANALYZE)
  - Reports table sizes, row counts, and bloat after maintenance

Usage:
    python scripts/db-maintenance.py
    make db-maintenance

Environment:
    DATABASE_URL   postgresql[+asyncpg]://user:pass@host:port/dbname
    PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE  (alternative to DATABASE_URL)

    REINDEX_BLOAT_RATIO   dead-tuple ratio threshold for reindex (default: 0.3)
    DRY_RUN               set to 'true' to print actions without executing
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg2
import psycopg2.extras


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

REINDEX_BLOAT_RATIO: float = float(os.getenv("REINDEX_BLOAT_RATIO", "0.3"))
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"


# --------------------------------------------------------------------------- #
# Connection helper
# --------------------------------------------------------------------------- #

def _parse_database_url(url: str) -> dict[str, Any]:
    """Parse a DATABASE_URL (including +asyncpg variants) into psycopg2 kwargs."""
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgres+asyncpg://", "postgresql://")
    p = urllib.parse.urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "user": p.username or "app",
        "password": urllib.parse.unquote(p.password or ""),
        "dbname": p.path.lstrip("/") or "enterprise_agents",
    }


def _get_conn_kwargs() -> dict[str, Any]:
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        return _parse_database_url(database_url)
    return {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "user": os.getenv("PGUSER", "app"),
        "password": os.getenv("PGPASSWORD", ""),
        "dbname": os.getenv("PGDATABASE", "enterprise_agents"),
    }


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class TableStats:
    schema: str
    table: str
    live_tuples: int
    dead_tuples: int
    dead_ratio: float
    total_size_bytes: int
    total_size_human: str
    table_size_human: str
    index_size_human: str
    last_vacuum: str | None
    last_autovacuum: str | None
    last_analyze: str | None
    reindexed: bool = False
    vacuumed: bool = False


@dataclass
class MaintenanceReport:
    started_at: str
    completed_at: str = ""
    duration_seconds: float = 0.0
    database: str = ""
    host: str = ""
    port: int = 5432
    dry_run: bool = DRY_RUN
    tables_vacuumed: int = 0
    tables_reindexed: int = 0
    tables: list[TableStats] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Maintenance functions
# --------------------------------------------------------------------------- #

def _get_table_stats(cur: Any) -> list[TableStats]:
    """Query pg_stat_user_tables and pg_class for bloat and size info."""
    cur.execute("""
        SELECT
            s.schemaname,
            s.relname                                               AS tablename,
            s.n_live_tup,
            s.n_dead_tup,
            CASE WHEN (s.n_live_tup + s.n_dead_tup) > 0
                 THEN round(s.n_dead_tup::numeric / (s.n_live_tup + s.n_dead_tup), 4)
                 ELSE 0
            END                                                    AS dead_ratio,
            pg_total_relation_size(s.relid)                        AS total_bytes,
            pg_size_pretty(pg_total_relation_size(s.relid))        AS total_size,
            pg_size_pretty(pg_relation_size(s.relid))              AS table_size,
            pg_size_pretty(
                pg_total_relation_size(s.relid)
                - pg_relation_size(s.relid)
            )                                                      AS index_size,
            s.last_vacuum::text,
            s.last_autovacuum::text,
            s.last_analyze::text
        FROM pg_stat_user_tables s
        WHERE s.schemaname = 'public'
        ORDER BY dead_ratio DESC, total_bytes DESC;
    """)
    rows = cur.fetchall()
    stats = []
    for row in rows:
        stats.append(TableStats(
            schema=row["schemaname"],
            table=row["tablename"],
            live_tuples=int(row["n_live_tup"]),
            dead_tuples=int(row["n_dead_tup"]),
            dead_ratio=float(row["dead_ratio"]),
            total_size_bytes=int(row["total_bytes"]),
            total_size_human=row["total_size"],
            table_size_human=row["table_size"],
            index_size_human=row["index_size"],
            last_vacuum=row["last_vacuum"],
            last_autovacuum=row["last_autovacuum"],
            last_analyze=row["last_analyze"],
        ))
    return stats


def _vacuum_analyze(conn: Any, schema: str, table: str, dry_run: bool) -> None:
    """Run VACUUM ANALYZE on a single table."""
    sql = f'VACUUM (ANALYZE, VERBOSE) "{schema}"."{table}"'
    if dry_run:
        print(f"  [DRY RUN] Would execute: {sql}")
        return
    # VACUUM cannot run inside a transaction block
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.set_isolation_level(old_isolation)


def _reindex_table(conn: Any, schema: str, table: str, dry_run: bool) -> None:
    """Reindex all indexes on a table concurrently to reclaim index bloat."""
    sql = f'REINDEX TABLE CONCURRENTLY "{schema}"."{table}"'
    if dry_run:
        print(f"  [DRY RUN] Would execute: {sql}")
        return
    # REINDEX CONCURRENTLY cannot run inside a transaction block
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except psycopg2.errors.FeatureNotSupported:
        # Older PG versions don't support CONCURRENTLY for REINDEX TABLE
        with conn.cursor() as cur:
            cur.execute(f'REINDEX TABLE "{schema}"."{table}"')
        conn.commit()
    finally:
        conn.set_isolation_level(old_isolation)


def _update_statistics(conn: Any, dry_run: bool) -> None:
    """Run ANALYZE on the whole database to refresh planner statistics."""
    sql = "ANALYZE VERBOSE"
    if dry_run:
        print(f"  [DRY RUN] Would execute: {sql}")
        return
    old_isolation = conn.isolation_level
    conn.set_isolation_level(0)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.set_isolation_level(old_isolation)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    conn_kwargs = _get_conn_kwargs()
    report = MaintenanceReport(
        started_at=datetime.datetime.utcnow().isoformat() + "Z",
        database=conn_kwargs["dbname"],
        host=conn_kwargs["host"],
        port=conn_kwargs["port"],
        dry_run=DRY_RUN,
    )

    print("=" * 64)
    print("Enterprise Agent Platform - Database Maintenance")
    print("=" * 64)
    print(f"Database : {conn_kwargs['dbname']} @ {conn_kwargs['host']}:{conn_kwargs['port']}")
    print(f"Dry run  : {DRY_RUN}")
    print(f"Reindex  : tables with dead_ratio >= {REINDEX_BLOAT_RATIO:.0%}")
    print()

    start_time = time.monotonic()

    try:
        conn = psycopg2.connect(
            cursor_factory=psycopg2.extras.RealDictCursor,
            **conn_kwargs,
        )
    except Exception as exc:
        print(f"ERROR: Cannot connect to database: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        with conn.cursor() as cur:
            stats = _get_table_stats(cur)

        print(f"Found {len(stats)} tables in public schema.\n")

        # ---- VACUUM ANALYZE all tables --------------------------------------
        print("Phase 1: VACUUM ANALYZE")
        print("-" * 40)
        for ts in stats:
            print(f"  VACUUM ANALYZE {ts.schema}.{ts.table}  "
                  f"(live={ts.live_tuples:,}  dead={ts.dead_tuples:,}  "
                  f"dead_ratio={ts.dead_ratio:.1%}  size={ts.total_size_human})")
            try:
                _vacuum_analyze(conn, ts.schema, ts.table, DRY_RUN)
                ts.vacuumed = True
                report.tables_vacuumed += 1
            except Exception as exc:
                msg = f"VACUUM failed on {ts.schema}.{ts.table}: {exc}"
                print(f"  WARNING: {msg}")
                report.errors.append(msg)
        print()

        # ---- REINDEX bloated tables -----------------------------------------
        print("Phase 2: REINDEX bloated tables")
        print("-" * 40)
        bloated = [t for t in stats if t.dead_ratio >= REINDEX_BLOAT_RATIO]
        if not bloated:
            print(f"  No tables exceed dead_ratio threshold ({REINDEX_BLOAT_RATIO:.0%}).")
        for ts in bloated:
            print(f"  REINDEX {ts.schema}.{ts.table}  (dead_ratio={ts.dead_ratio:.1%})")
            try:
                _reindex_table(conn, ts.schema, ts.table, DRY_RUN)
                ts.reindexed = True
                report.tables_reindexed += 1
            except Exception as exc:
                msg = f"REINDEX failed on {ts.schema}.{ts.table}: {exc}"
                print(f"  WARNING: {msg}")
                report.errors.append(msg)
        print()

        # ---- Global ANALYZE -------------------------------------------------
        print("Phase 3: Update planner statistics (ANALYZE)")
        print("-" * 40)
        try:
            _update_statistics(conn, DRY_RUN)
            print("  ANALYZE complete." if not DRY_RUN else "  [DRY RUN] Skipped.")
        except Exception as exc:
            msg = f"ANALYZE failed: {exc}"
            print(f"  WARNING: {msg}")
            report.errors.append(msg)
        print()

        # ---- Re-fetch stats post-maintenance --------------------------------
        if not DRY_RUN:
            with conn.cursor() as cur:
                stats = _get_table_stats(cur)

        report.tables = stats

    finally:
        conn.close()

    # ---- Report -------------------------------------------------------------
    elapsed = time.monotonic() - start_time
    report.completed_at = datetime.datetime.utcnow().isoformat() + "Z"
    report.duration_seconds = round(elapsed, 2)

    print("=" * 64)
    print("Maintenance Summary")
    print("=" * 64)
    print(f"  Duration   : {elapsed:.1f}s")
    print(f"  Vacuumed   : {report.tables_vacuumed} tables")
    print(f"  Reindexed  : {report.tables_reindexed} tables")
    print(f"  Errors     : {len(report.errors)}")
    print()

    print("Table Sizes (post-maintenance, top 20 by total size):")
    print(f"  {'Table':<40} {'Total':>10} {'Rows':>12} {'Dead%':>7}")
    print("  " + "-" * 72)
    for ts in sorted(stats, key=lambda t: t.total_size_bytes, reverse=True)[:20]:
        print(f"  {ts.schema}.{ts.table:<38} {ts.total_size_human:>10} "
              f"{ts.live_tuples:>12,} {ts.dead_ratio:>7.1%}")

    if report.errors:
        print()
        print("Errors:")
        for err in report.errors:
            print(f"  - {err}")

    # ---- JSON report (for machine consumption) --------------------------------
    report_path = os.path.join(
        os.getcwd(),
        f"db_maintenance_{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.json",
    )
    report_dict = asdict(report)
    # Convert TableStats list to plain dicts
    report_dict["tables"] = [asdict(t) for t in report.tables]

    with open(report_path, "w") as f:
        json.dump(report_dict, f, indent=2, default=str)

    print()
    print(f"Full JSON report: {report_path}")

    if report.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
