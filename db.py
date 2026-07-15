import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "dynasty.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                player_id   TEXT PRIMARY KEY,
                full_name   TEXT,
                position    TEXT,
                team        TEXT,
                age         REAL,
                years_exp   INTEGER
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id      TEXT PRIMARY KEY,
                display_name TEXT,
                team_name    TEXT
            );

            CREATE TABLE IF NOT EXISTS rosters (
                roster_id INTEGER PRIMARY KEY,
                owner_id  TEXT
            );

            -- slot: 'active' | 'taxi' | 'ir'
            CREATE TABLE IF NOT EXISTS roster_players (
                roster_id INTEGER,
                player_id TEXT,
                slot      TEXT,
                PRIMARY KEY (roster_id, player_id)
            );

            -- Sleeper API: roster_id = original team, owner_id = current holder
            CREATE TABLE IF NOT EXISTS traded_picks (
                season             TEXT,
                round              INTEGER,
                original_roster_id INTEGER,  -- Sleeper roster_id: team whose pick slot this is
                current_roster_id  INTEGER,  -- Sleeper owner_id: team that currently holds it
                PRIMARY KEY (season, round, original_roster_id)
            );

            -- Sleeper trade/waiver/FA transactions, keyed by transaction_id.
            -- Rebuilt each ingest run via INSERT OR REPLACE.
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id  TEXT PRIMARY KEY,
                type            TEXT,
                status          TEXT,
                season          TEXT,
                week            INTEGER,
                roster_ids      TEXT,   -- JSON array of roster_ids involved
                adds            TEXT,   -- JSON: player_id -> new_roster_id
                drops           TEXT,   -- JSON: player_id -> old_roster_id
                draft_picks     TEXT,   -- JSON array of pick objects
                created         INTEGER -- Unix timestamp ms
            );

            -- Append-only: never UPDATE or DELETE rows. Enables value trend tracking later.
            CREATE TABLE IF NOT EXISTS fc_values (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id  TEXT    NOT NULL,
                name       TEXT,
                position   TEXT,
                value      INTEGER NOT NULL,
                fetched_at TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS league_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    # Schema migrations — idempotent, safe to re-run
    for col_sql in [
        "ALTER TABLE rosters ADD COLUMN wins    INTEGER DEFAULT 0",
        "ALTER TABLE rosters ADD COLUMN losses  INTEGER DEFAULT 0",
        "ALTER TABLE rosters ADD COLUMN ties    INTEGER DEFAULT 0",
        "ALTER TABLE rosters ADD COLUMN fpts    REAL    DEFAULT 0",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass  # column already exists

    conn.close()
