from __future__ import annotations

import unicodedata

from deskweave.errors import DeskWeaveError


def initialize_run_views(connection):
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_catalog_version'"
    ).fetchone()
    for statement in (
        "CREATE INDEX IF NOT EXISTS execution_inspection_names ON "
        "execution_value(run_id,frame_id,scope,name COLLATE UTF16,version)",
        "CREATE TABLE IF NOT EXISTS run_catalog_version (sequence INTEGER PRIMARY KEY "
        "AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES run(id),name TEXT NOT NULL,"
        "name_key TEXT NOT NULL,revision_id TEXT NOT NULL,state TEXT NOT NULL,"
        "updated_at TEXT NOT NULL,valid_until INTEGER)",
        "CREATE UNIQUE INDEX IF NOT EXISTS run_catalog_current "
        "ON run_catalog_version(run_id) WHERE valid_until IS NULL",
        "CREATE INDEX IF NOT EXISTS run_catalog_updated ON "
        "run_catalog_version(updated_at DESC,run_id ASC,sequence DESC)",
        "CREATE INDEX IF NOT EXISTS run_catalog_name_asc ON "
        "run_catalog_version(name_key ASC,run_id ASC,sequence DESC)",
        "CREATE INDEX IF NOT EXISTS run_catalog_name_desc ON "
        "run_catalog_version(name_key DESC,run_id ASC,sequence DESC)",
        "CREATE TABLE IF NOT EXISTS run_frame_version (run_id TEXT NOT NULL,"
        "frame_id TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,"
        "node_id TEXT NOT NULL,valid_until INTEGER,PRIMARY KEY(run_id,frame_id,version),"
        "FOREIGN KEY(run_id,frame_id) REFERENCES frame(run_id,id))",
        "CREATE UNIQUE INDEX IF NOT EXISTS run_frame_current "
        "ON run_frame_version(run_id,frame_id) WHERE valid_until IS NULL",
        "CREATE TABLE IF NOT EXISTS run_metrics (run_id TEXT PRIMARY KEY REFERENCES run(id),"
        "frame_count INTEGER NOT NULL)",
        "CREATE INDEX IF NOT EXISTS run_state_sequence ON run(state,sequence)",
        "CREATE INDEX IF NOT EXISTS frame_unfinished ON frame(run_id,sequence) "
        "WHERE state NOT IN ('succeeded','failed','cancelled')",
    ):
        connection.execute(statement)
    if existing is None:
        connection.execute(
            "INSERT INTO run_catalog_version(run_id,name,name_key,revision_id,state,updated_at) "
            "SELECT r.id,v.name,fold(v.name),r.revision_id,r.state,r.updated_at "
            "FROM run r JOIN revision v ON v.id=r.revision_id ORDER BY r.sequence"
        )
        connection.execute(
            "INSERT INTO run_frame_version(run_id,frame_id,version,state,node_id) "
            "SELECT f.run_id,f.id,r.data_version,f.state,f.node_id "
            "FROM frame f JOIN run r ON r.id=f.run_id"
        )
        connection.execute(
            "INSERT INTO run_metrics SELECT r.id,COUNT(f.id) FROM run r "
            "LEFT JOIN frame f ON f.run_id=r.id GROUP BY r.id"
        )


def record_run(connection, run_id, state, updated_at, *, name=None, revision=None):
    current = connection.execute(
        "SELECT name,revision_id,state,updated_at FROM run_catalog_version "
        "WHERE run_id=? AND valid_until IS NULL",
        (run_id,),
    ).fetchone()
    if current is not None:
        if current["state"] == state and current["updated_at"] == updated_at:
            return
        name, revision = current["name"], current["revision_id"]
    elif name is None or revision is None:
        raise DeskWeaveError("STORAGE_CORRUPT", "运行目录记录缺失。")
    sequence = connection.execute(
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='run_catalog_version'),0)+1"
    ).fetchone()[0]
    connection.execute(
        "UPDATE run_catalog_version SET valid_until=? WHERE run_id=? AND valid_until IS NULL",
        (sequence, run_id),
    )
    connection.execute(
        "INSERT INTO run_catalog_version(sequence,run_id,name,name_key,revision_id,state,"
        "updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            sequence,
            run_id,
            name,
            unicodedata.normalize("NFC", name).casefold(),
            revision,
            state,
            updated_at,
        ),
    )
    connection.execute(
        "INSERT INTO run_metrics VALUES(?,0) ON CONFLICT(run_id) DO NOTHING", (run_id,)
    )


def record_frame(connection, run_id, frame_id, version, state, node_id):
    current = connection.execute(
        "SELECT version,state,node_id FROM run_frame_version "
        "WHERE run_id=? AND frame_id=? AND valid_until IS NULL",
        (run_id, frame_id),
    ).fetchone()
    if current is not None:
        if (current["state"], current["node_id"]) == (state, node_id):
            return
        if version <= current["version"]:
            raise DeskWeaveError("REVISION_CONFLICT", "调用帧数据版本已变化。")
        connection.execute(
            "UPDATE run_frame_version SET valid_until=? "
            "WHERE run_id=? AND frame_id=? AND valid_until IS NULL",
            (version, run_id, frame_id),
        )
    else:
        connection.execute(
            "UPDATE run_metrics SET frame_count=frame_count+1 WHERE run_id=?", (run_id,)
        )
    connection.execute(
        "INSERT INTO run_frame_version(run_id,frame_id,version,state,node_id) VALUES(?,?,?,?,?)",
        (run_id, frame_id, version, state, node_id),
    )
