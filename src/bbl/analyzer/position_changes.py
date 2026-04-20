"""Detect position changes by comparing consecutive position snapshots."""

from __future__ import annotations

import logging

from bbl.storage import Database

log = logging.getLogger(__name__)

# language=sql
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS position_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    address     TEXT NOT NULL,
    condition_id TEXT,
    token_id    TEXT,
    outcome     TEXT,
    change_type TEXT NOT NULL,
    old_size    REAL,
    new_size    REAL,
    size_delta  REAL,
    old_value   REAL,
    new_value   REAL
);
CREATE INDEX IF NOT EXISTS idx_pc_ts      ON position_changes(ts DESC);
CREATE INDEX IF NOT EXISTS idx_pc_addr_ts ON position_changes(address, ts);
CREATE INDEX IF NOT EXISTS idx_pc_cond_ts ON position_changes(condition_id, ts);
"""

# Find all addresses that have >=2 distinct snapshot timestamps, but only
# consider snapshots newer than whatever we already processed for that address.
# language=sql
_ADDRESSES_WITH_SNAPSHOTS = """
SELECT DISTINCT p.address
FROM positions p
WHERE (
    SELECT COUNT(DISTINCT p2.ts)
    FROM positions p2
    WHERE p2.address = p.address
) >= 2
"""

# For a given address, get the two most recent distinct timestamps.
# language=sql
_LATEST_TWO_TS = """
SELECT DISTINCT ts
FROM positions
WHERE address = ?
ORDER BY ts DESC
LIMIT 2
"""

# All token positions for a given (address, ts).
# language=sql
_SNAPSHOT_AT = """
SELECT token_id, condition_id, outcome, size, current_value
FROM positions
WHERE address = ? AND ts = ?
"""

# Max ts already stored for a given address in position_changes.
# language=sql
_MAX_EXISTING_TS = """
SELECT COALESCE(MAX(ts), 0) FROM position_changes WHERE address = ?
"""

# language=sql
_INSERT_CHANGE = """
INSERT INTO position_changes (
    ts, address, condition_id, token_id, outcome,
    change_type, old_size, new_size, size_delta,
    old_value, new_value
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def detect_position_changes(db: Database) -> int:
    """Compare the two most recent position snapshots per trader and record changes.

    Returns the number of changes detected.
    """
    db.conn.executescript(_CREATE_TABLE)

    total = 0

    addresses = [
        row[0] for row in db.conn.execute(_ADDRESSES_WITH_SNAPSHOTS).fetchall()
    ]

    for address in addresses:
        ts_rows = db.conn.execute(_LATEST_TWO_TS, (address,)).fetchall()
        if len(ts_rows) < 2:
            continue

        new_ts = ts_rows[0][0]
        old_ts = ts_rows[1][0]

        # Skip if we already processed this snapshot pair.
        (max_existing,) = db.conn.execute(_MAX_EXISTING_TS, (address,)).fetchone()
        if new_ts <= max_existing:
            continue

        # Build lookup dicts keyed by token_id.
        old_rows = db.conn.execute(_SNAPSHOT_AT, (address, old_ts)).fetchall()
        new_rows = db.conn.execute(_SNAPSHOT_AT, (address, new_ts)).fetchall()

        old_map: dict[str, tuple] = {}
        for token_id, condition_id, outcome, size, value in old_rows:
            old_map[token_id] = (condition_id, outcome, size or 0.0, value or 0.0)

        new_map: dict[str, tuple] = {}
        for token_id, condition_id, outcome, size, value in new_rows:
            new_map[token_id] = (condition_id, outcome, size or 0.0, value or 0.0)

        all_tokens = set(old_map) | set(new_map)

        for token_id in all_tokens:
            old_cond, old_outcome, old_size, old_value = old_map.get(
                token_id, (None, None, 0.0, 0.0)
            )
            new_cond, new_outcome, new_size, new_value = new_map.get(
                token_id, (None, None, 0.0, 0.0)
            )

            condition_id = new_cond or old_cond
            outcome = new_outcome or old_outcome

            change_type: str | None = None

            if old_size == 0 and new_size > 0:
                change_type = "entry"
            elif old_size > 0 and new_size == 0:
                change_type = "exit"
            elif old_size > 0 and new_size > 0:
                ratio = new_size / old_size
                if ratio > 1.20:
                    change_type = "increase"
                elif ratio < 0.80:
                    change_type = "decrease"

            if change_type is None:
                continue

            db.conn.execute(
                _INSERT_CHANGE,
                (
                    new_ts,
                    address,
                    condition_id,
                    token_id,
                    outcome,
                    change_type,
                    old_size,
                    new_size,
                    new_size - old_size,
                    old_value,
                    new_value,
                ),
            )
            total += 1

    db.conn.commit()
    log.info("detected position changes: %d rows", total)
    return total
