"""SQLite 持久层：版本化的步道图与封闭记录，原子发布。

设计要点
--------
* 每次导入生成一个新版本（``graph_versions`` / ``closure_versions``），
  旧版本原样保留；发布动作在单个事务内把版本指针
  （``current_versions``）切到新版本，对查询而言是原子的。
* 所有数据在导入时先做完整校验，任何错误都不写入，旧版本继续可用。
* 查询开始时先读取指针中的版本号，随后所有读取都显式带上该版本号，
  保证单次查询只使用同一版本（即便导入正在进行）。
* 写操作在进程内再用一把互斥锁串行化，配合 SQLite 事务，避免并发写入。
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sqlite3
import threading
from typing import Any

from .timeutil import canonical, is_aware, parse_datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_versions (
    version    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    node_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    version     INTEGER NOT NULL REFERENCES graph_versions(version),
    node_id     TEXT NOT NULL,
    PRIMARY KEY (version, node_id)
);

CREATE TABLE IF NOT EXISTS edges (
    version     INTEGER NOT NULL REFERENCES graph_versions(version),
    edge_id     TEXT NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    seconds     INTEGER NOT NULL CHECK (seconds > 0),
    stairs      INTEGER NOT NULL CHECK (stairs IN (0, 1)),
    seq         INTEGER NOT NULL,
    PRIMARY KEY (version, edge_id)
);

CREATE TABLE IF NOT EXISTS closure_versions (
    version       INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    graph_version INTEGER NOT NULL REFERENCES graph_versions(version),
    closure_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS closures (
    closure_version INTEGER NOT NULL REFERENCES closure_versions(version),
    seq             INTEGER NOT NULL,
    edge_id         TEXT NOT NULL,
    start_time      TEXT NOT NULL,
    end_time        TEXT NOT NULL,
    PRIMARY KEY (closure_version, seq)
);

CREATE TABLE IF NOT EXISTS current_versions (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    graph_version   INTEGER REFERENCES graph_versions(version),
    closure_version INTEGER REFERENCES closure_versions(version)
);

INSERT OR IGNORE INTO current_versions (id, graph_version, closure_version)
VALUES (1, NULL, NULL);
"""

_write_lock = threading.Lock()


class ValidationError(ValueError):
    """导入数据未通过校验（旧版本会被保留）。"""


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(
        db_path, timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_graph(payload: Any) -> tuple[list[str], list[dict]]:
    """校验步道图导入体，返回 ``(node_ids, edges)``。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象，包含 nodes 与 edges")

    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValidationError("nodes 与 edges 必须都是数组")

    node_ids: list[str] = []
    seen_nodes: set[str] = set()
    for i, node in enumerate(raw_nodes):
        if isinstance(node, int) and not isinstance(node, bool):
            nid = str(node)
        elif _is_nonempty_str(node):
            nid = node.strip()
        elif isinstance(node, dict):
            raw_id = node.get("id")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                nid = str(raw_id)
            elif _is_nonempty_str(raw_id):
                nid = str(raw_id).strip()
            else:
                nid = ""
        else:
            nid = ""
        if not nid:
            errors.append(f"nodes[{i}] 必须是非空字符串/整数，或含非空 id 的对象")
            continue
        if nid in seen_nodes:
            errors.append(f"节点 {nid!r} 重复")
        else:
            seen_nodes.add(nid)
            node_ids.append(nid)

    edges: list[dict] = []
    seen_edges: set[str] = set()
    for i, edge in enumerate(raw_edges):
        prefix = f"edges[{i}]"
        if not isinstance(edge, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        eid = edge.get("id")
        if isinstance(eid, int) and not isinstance(eid, bool):
            eid = str(eid)
        if not _is_nonempty_str(eid):
            errors.append(f"{prefix}.id 必须是非空字符串或整数")
            continue
        eid = str(eid).strip()
        if eid in seen_edges:
            errors.append(f"{prefix} 路段编号 {eid!r} 重复")
            continue

        src = edge.get("source")
        dst = edge.get("target")
        if isinstance(src, int) and not isinstance(src, bool):
            src = str(src)
        if isinstance(dst, int) and not isinstance(dst, bool):
            dst = str(dst)
        if not _is_nonempty_str(src):
            errors.append(f"{prefix}.source 必须是非空节点编号")
        else:
            src = src.strip()
            if src not in seen_nodes:
                errors.append(f"{prefix}.source 引用未知节点 {src!r}")
        if not _is_nonempty_str(dst):
            errors.append(f"{prefix}.target 必须是非空节点编号")
        else:
            dst = dst.strip()
            if dst not in seen_nodes:
                errors.append(f"{prefix}.target 引用未知节点 {dst!r}")

        seconds = edge.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            errors.append(f"{prefix}.seconds 必须是正整数（通行秒数）")

        stairs = edge.get("stairs", False)
        if not isinstance(stairs, bool):
            errors.append(f"{prefix}.stairs 必须是布尔值")

        seen_edges.add(eid)
        edges.append(
            {
                "id": eid,
                "source": src,
                "target": dst,
                "seconds": seconds,
                "stairs": 1 if stairs else 0,
            }
        )

    if errors:
        raise ValidationError("；".join(errors))
    return node_ids, edges


def validate_closures(
    payload: Any, known_edges: set[str]
) -> tuple[list[dict], bool]:
    """校验封闭记录导入体，返回 ``(rows, aware)``。

    ``known_edges`` 为当前已发布图中的路段编号集合；封闭记录只能引用
    当前图中存在的路段。所有时间区间必须同为带时区或同为不带时区。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象，包含 closures 数组")
    raw = payload.get("closures")
    if not isinstance(raw, list):
        raise ValidationError("closures 必须是数组")

    rows: list[dict] = []
    awareness: bool | None = None
    for i, item in enumerate(raw):
        prefix = f"closures[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        eid = item.get("edge_id")
        if isinstance(eid, int) and not isinstance(eid, bool):
            eid = str(eid)
        if not _is_nonempty_str(eid):
            errors.append(f"{prefix}.edge_id 必须是非空字符串或整数")
        else:
            eid = str(eid).strip()
            if eid not in known_edges:
                errors.append(f"{prefix}.edge_id 引用当前图中不存在的路段 {eid!r}")

        try:
            start = parse_datetime(item.get("start"), f"{prefix}.start")
            end = parse_datetime(item.get("end"), f"{prefix}.end")
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if is_aware(start) != is_aware(end):
            errors.append(f"{prefix} 的开始与结束时间必须同时带时区或同时不带")
            continue
        if start >= end:
            errors.append(f"{prefix} 时间区间无效：开始必须严格早于结束（半开区间）")
            continue

        if awareness is None:
            awareness = is_aware(start)
        elif awareness != is_aware(start):
            errors.append(f"{prefix} 与其他记录的时区性质不一致（带/不带时区不得混用）")

        rows.append({"edge_id": eid, "start": canonical(start), "end": canonical(end)})

    if errors:
        raise ValidationError("；".join(errors))
    return rows, bool(awareness)


# ---------------------------------------------------------------------------
# 原子发布
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish_graph(conn: sqlite3.Connection, payload: Any) -> dict:
    """校验并原子发布新步道图版本。

    发布新图后封闭指针被清空（旧封闭引用的可能是旧图的路段，
    需要重新导入封闭记录），已发布的图版本本身保留在历史中。
    """
    node_ids, edges = validate_graph(payload)
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO graph_versions (created_at, node_count, edge_count) "
                "VALUES (?, ?, ?)",
                (_now_iso(), len(node_ids), len(edges)),
            )
            version = cur.lastrowid
            conn.executemany(
                "INSERT INTO nodes (version, node_id) VALUES (?, ?)",
                [(version, nid) for nid in node_ids],
            )
            conn.executemany(
                "INSERT INTO edges (version, edge_id, source, target, seconds, stairs, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (version, e["id"], e["source"], e["target"], e["seconds"],
                     e["stairs"], idx)
                    for idx, e in enumerate(edges)
                ],
            )
            conn.execute(
                "UPDATE current_versions SET graph_version = ?, closure_version = NULL "
                "WHERE id = 1",
                (version,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "graph_version": version,
        "node_count": len(node_ids),
        "edge_count": len(edges),
    }


def publish_closures(conn: sqlite3.Connection, payload: Any) -> dict:
    """校验并原子发布新封闭记录版本（针对当前发布的图）。"""
    current = get_current_versions(conn)
    if current["graph_version"] is None:
        raise ValidationError("尚未发布任何步道图，无法导入封闭记录")
    graph_version = current["graph_version"]
    known = {
        row["edge_id"]
        for row in conn.execute(
            "SELECT edge_id FROM edges WHERE version = ?", (graph_version,)
        )
    }
    rows, _aware = validate_closures(payload, known)

    with _write_lock:
        # 再次确认指针未在锁外被改动
        current2 = get_current_versions(conn)
        if current2["graph_version"] != graph_version:
            raise ValidationError("步道图在导入期间被更新，请针对新图重新导入封闭记录")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO closure_versions (created_at, graph_version, closure_count) "
                "VALUES (?, ?, ?)",
                (_now_iso(), graph_version, len(rows)),
            )
            version = cur.lastrowid
            conn.executemany(
                "INSERT INTO closures (closure_version, seq, edge_id, start_time, end_time) "
                "VALUES (?, ?, ?, ?, ?)",
                [(version, idx, r["edge_id"], r["start"], r["end"])
                 for idx, r in enumerate(rows)],
            )
            conn.execute(
                "UPDATE current_versions SET closure_version = ? WHERE id = 1",
                (version,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"closure_version": version, "closure_count": len(rows),
            "graph_version": graph_version}


# ---------------------------------------------------------------------------
# 读取（版本快照）
# ---------------------------------------------------------------------------


def get_current_versions(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT graph_version, closure_version FROM current_versions WHERE id = 1"
    ).fetchone()
    return {"graph_version": row["graph_version"],
            "closure_version": row["closure_version"]}


def load_snapshot(conn: sqlite3.Connection) -> dict:
    """读取当前版本指针及该版本下的全部节点、路段、封闭记录。

    先取版本号，再按显式版本号读取所有数据，保证快照内版本一致。
    """
    versions = get_current_versions(conn)
    gv = versions["graph_version"]
    cv = versions["closure_version"]
    if gv is None:
        return {"graph_version": None, "closure_version": None,
                "nodes": [], "edges": [], "closures": []}

    nodes = [
        row["node_id"]
        for row in conn.execute(
            "SELECT node_id FROM nodes WHERE version = ? ORDER BY node_id", (gv,)
        )
    ]
    edges = [
        {
            "id": row["edge_id"],
            "source": row["source"],
            "target": row["target"],
            "seconds": row["seconds"],
            "stairs": bool(row["stairs"]),
        }
        for row in conn.execute(
            "SELECT edge_id, source, target, seconds, stairs "
            "FROM edges WHERE version = ? ORDER BY seq",
            (gv,),
        )
    ]
    closures: list[dict] = []
    if cv is not None:
        closures = [
            {
                "edge_id": row["edge_id"],
                "start": row["start_time"],
                "end": row["end_time"],
            }
            for row in conn.execute(
                "SELECT edge_id, start_time, end_time "
                "FROM closures WHERE closure_version = ? ORDER BY seq",
                (cv,),
            )
        ]
    return {"graph_version": gv, "closure_version": cv,
            "nodes": nodes, "edges": edges, "closures": closures}
