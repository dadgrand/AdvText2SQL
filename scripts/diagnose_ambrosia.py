"""Read-only catalog checks for Ambrosia databases missing from historical runs."""

import json
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import URL

from baseline_connection import database_engine

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = dotenv_values(ROOT / ".env.openrouter")
    dbs = ["attachment_1tab_ref_seating_area", "scope_law_firms_legal_positions",
           "vague_2cols_rating2", "attachment_1tab_ref_dining"]
    reports = []
    for db in dbs:
        result = {"database": db}
        engine = database_engine(URL.create(
            "postgresql+psycopg", username=config["DB_USER"], password=config["DB_PASS"],
            host="127.0.0.1", port=5444, database=db,
        ))
        try:
            with engine.connect() as conn:
                result["role"] = dict(conn.execute(text("""
                    SELECT current_database() AS database, current_user AS username,
                           current_setting('search_path') AS search_path,
                           current_setting('transaction_read_only') AS read_only,
                           pg_has_role(current_user, 'pg_read_all_data', 'member') AS read_all_data
                """)).mappings().one())
                result["schemas"] = [dict(row) for row in conn.execute(text("""
                    SELECT nspname AS name, has_schema_privilege(oid, 'USAGE') AS can_use
                    FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
                      AND nspname != 'information_schema' ORDER BY nspname
                """)).mappings()]
                result["relations"] = [dict(row) for row in conn.execute(text("""
                    SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
                           has_table_privilege(c.oid, 'SELECT') AS can_select
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname != 'information_schema'
                      AND c.relkind IN ('r','p','v','m','f')
                    ORDER BY n.nspname,c.relname
                """)).mappings()]
                result["status"] = "ok"
        except Exception as error:
            result.update(status="failed", error_type=type(error).__name__)
        finally:
            engine.dispose()
        reports.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    output = ROOT / "research_results/runs" / "gpt54mini_baseline_large_20261016" / "ambrosia" / "database_diagnostics.json"
    output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
