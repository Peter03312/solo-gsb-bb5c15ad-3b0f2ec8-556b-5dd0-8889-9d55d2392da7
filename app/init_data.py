"""初始化数据：若数据库中尚未发布步道图，则导入种子文件。

文件路径通过环境变量配置：
``PARK_SEED_GRAPH``（默认 ``seed/graph.json``）与
``PARK_SEED_CLOSURES``（默认 ``seed/closures.json``）。
设为 ``skip`` 可跳过对应导入。
重复执行是安全的：已有已发布图时不会重复导入。
"""

from __future__ import annotations

import json
import os

from . import db


def seed_if_empty(conn, graph_path: str | None = None,
                  closures_path: str | None = None) -> bool:
    """数据库为空时导入种子数据。返回是否执行了导入。"""
    graph_path = graph_path or os.environ.get("PARK_SEED_GRAPH", "seed/graph.json")
    closures_path = closures_path or os.environ.get(
        "PARK_SEED_CLOSURES", "seed/closures.json"
    )

    current = db.get_current_versions(conn)
    if current["graph_version"] is not None:
        return False

    with open(graph_path, "r", encoding="utf-8") as fh:
        graph_payload = json.load(fh)
    graph_result = db.publish_graph(conn, graph_payload)
    print(f"[init] 已导入步道图版本 {graph_result['graph_version']} "
          f"({graph_result['node_count']} 个节点, "
          f"{graph_result['edge_count']} 条路段)")

    if closures_path != "skip" and os.path.exists(closures_path):
        with open(closures_path, "r", encoding="utf-8") as fh:
            closures_payload = json.load(fh)
        try:
            closure_result = db.publish_closures(conn, closures_payload)
            print(f"[init] 已导入封闭记录版本 "
                  f"{closure_result['closure_version']} "
                  f"({closure_result['closure_count']} 条)")
        except db.ValidationError as exc:
            print(f"[init] 种子封闭记录未导入：{exc}")
    return True


if __name__ == "__main__":
    conn = db.connect(os.environ.get("PARK_DB_PATH", "/data/park.db"))
    seed_if_empty(conn)
