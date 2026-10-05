"""HTTP API：步道图 / 封闭记录导入与通行规划查询。

路由
----
``GET  /health``                 健康检查
``PUT  /api/graph``              原子发布新步道图版本
``PUT  /api/closures``           原子发布新封闭记录版本
``GET  /api/versions``           查看当前生效版本
``POST /api/route``              查询最早到达路线
"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, jsonify, request

from . import db
from .routing import Closure, Edge, Graph, find_route
from .timeutil import format_dt, parse_datetime, same_awareness


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    db_path = db_path or os.environ.get("PARK_DB_PATH", "/data/park.db")
    conn = db.connect(db_path)
    app.config["DB_CONN"] = conn

    def error(message: str, status: int = 400, **extra):
        payload = {"ok": False, "error": message}
        payload.update(extra)
        return jsonify(payload), status

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "park-route-planner"})

    @app.put("/api/graph")
    def put_graph():
        payload = request.get_json(silent=True)
        try:
            result = db.publish_graph(conn, payload)
        except db.ValidationError as exc:
            return error(f"导入被拒绝，保留旧版本：{exc}", 422)
        except sqlite3.OperationalError as exc:
            return error(f"数据库繁忙，请稍后重试：{exc}", 503)
        except sqlite3.Error as exc:
            return error(f"数据库错误：{exc}", 500)
        return jsonify({"ok": True, **result})

    @app.put("/api/closures")
    def put_closures():
        payload = request.get_json(silent=True)
        try:
            result = db.publish_closures(conn, payload)
        except db.ValidationError as exc:
            return error(f"导入被拒绝，保留旧版本：{exc}", 422)
        except sqlite3.OperationalError as exc:
            return error(f"数据库繁忙，请稍后重试：{exc}", 503)
        except sqlite3.Error as exc:
            return error(f"数据库错误：{exc}", 500)
        return jsonify({"ok": True, **result})

    @app.get("/api/versions")
    def versions():
        return jsonify({"ok": True, **db.get_current_versions(conn)})

    @app.post("/api/route")
    def route():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            if request.content_type and not request.is_json:
                return error("Content-Type 必须为 application/json 且请求体为 JSON 对象")
            return error("请求体必须是 JSON 对象")

        origin = payload.get("origin")
        destination = payload.get("destination")
        depart_raw = payload.get("depart_at")
        avoid_stairs = payload.get("avoid_stairs", False)

        if not isinstance(origin, str) or not origin.strip():
            return error("origin 必须是非空节点编号")
        if not isinstance(destination, str) or not destination.strip():
            return error("destination 必须是非空节点编号")
        if not isinstance(depart_raw, str) or not depart_raw.strip():
            return error("depart_at 必须是 ISO 8601 出发时间字符串")
        if not isinstance(avoid_stairs, bool):
            return error("avoid_stairs 必须是布尔值")

        try:
            depart_at = parse_datetime(depart_raw, "depart_at")
        except ValueError as exc:
            return error(str(exc))

        # 读取一致版本快照：先取版本号，再按版本号读取
        snap = db.load_snapshot(conn)
        if snap["graph_version"] is None:
            return error("尚未发布步道图，无法规划路线", 409)

        origin = origin.strip()
        destination = destination.strip()
        unknown = [n for n in (origin, destination) if n not in set(snap["nodes"])]
        if unknown:
            return error(
                f"节点 {', '.join(repr(n) for n in unknown)} 不存在",
                404,
                unknown_nodes=unknown,
            )

        graph = Graph(nodes=set(snap["nodes"]))
        for e in snap["edges"]:
            edge = Edge(
                edge_id=e["id"], source=e["source"], target=e["target"],
                seconds=e["seconds"], stairs=e["stairs"],
            )
            graph.adjacency.setdefault(edge.source, []).append(edge)
        for n in snap["nodes"]:
            graph.adjacency.setdefault(n, [])

        closures = []
        try:
            for c in snap["closures"]:
                start = parse_datetime(c["start"], "封闭开始时间")
                end = parse_datetime(c["end"], "封闭结束时间")
                if not same_awareness(start, depart_at):
                    return error(
                        "封闭记录的时区性质（带/不带时区）与出发时间不一致，"
                        "请使用相同形式的出发时间",
                        422,
                    )
                closures.append(Closure(edge_id=c["edge_id"], start=start, end=end))
        except ValueError as exc:
            return error(str(exc), 500)

        try:
            result = find_route(
                graph, closures, origin, destination, depart_at, avoid_stairs
            )
        except KeyError as exc:
            return error(f"节点 {exc.args[0]!r} 不存在", 404)
        except ValueError as exc:
            return error(str(exc), 422)

        if result is None:
            return jsonify({
                "ok": True,
                "feasible": False,
                "origin": origin,
                "destination": destination,
                "depart_at": format_dt(depart_at),
                "avoid_stairs": avoid_stairs,
                "graph_version": snap["graph_version"],
                "closure_version": snap["closure_version"],
                "message": "在给定出发时刻与避台阶条件下无可行路线",
            })

        arrival, path, schedule = result
        legs = [
            {
                "edge_id": edge.edge_id,
                "from_node": edge.source,
                "to_node": edge.target,
                "seconds": edge.seconds,
                "stairs": edge.stairs,
                "enter_at": format_dt(enter_at),
                "leave_at": format_dt(leave_at),
            }
            for edge, enter_at, leave_at in schedule
        ]
        # 在节点处的等候秒数 = 本段进入时刻 - 上段离开时刻（首段相对出发时刻）
        prev_leave = depart_at
        for (_, enter_at, leave_at), item in zip(schedule, legs):
            item["waited_seconds"] = int((enter_at - prev_leave).total_seconds())
            prev_leave = leave_at

        return jsonify({
            "ok": True,
            "feasible": True,
            "origin": origin,
            "destination": destination,
            "depart_at": format_dt(depart_at),
            "arrive_at": format_dt(arrival),
            "total_seconds": int((arrival - depart_at).total_seconds()),
            "avoid_stairs": avoid_stairs,
            "edge_path": list(path),
            "legs": legs,
            "graph_version": snap["graph_version"],
            "closure_version": snap["closure_version"],
        })

    return app
