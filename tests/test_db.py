"""数据库版本化与校验的单元测试。"""

import os
import tempfile
import unittest

from app import db


VALID_GRAPH = {
    "nodes": ["A", "B", "C"],
    "edges": [
        {"id": "E1", "source": "A", "target": "B", "seconds": 60, "stairs": False},
        {"id": "E2", "source": "B", "target": "C", "seconds": 90, "stairs": True},
    ],
}


class ValidationTests(unittest.TestCase):
    def test_valid_graph(self):
        nodes, edges = db.validate_graph(VALID_GRAPH)
        self.assertEqual(nodes, ["A", "B", "C"])
        self.assertEqual(len(edges), 2)

    def test_reject_unknown_node_reference(self):
        payload = {
            "nodes": ["A"],
            "edges": [{"id": "E1", "source": "A", "target": "Z",
                       "seconds": 10, "stairs": False}],
        }
        with self.assertRaises(db.ValidationError) as ctx:
            db.validate_graph(payload)
        self.assertIn("未知节点", str(ctx.exception))

    def test_reject_non_positive_seconds(self):
        payload = {
            "nodes": ["A", "B"],
            "edges": [{"id": "E1", "source": "A", "target": "B",
                       "seconds": 0, "stairs": False}],
        }
        with self.assertRaises(db.ValidationError):
            db.validate_graph(payload)
        payload["edges"][0]["seconds"] = -5
        with self.assertRaises(db.ValidationError):
            db.validate_graph(payload)

    def test_reject_duplicate_edge_and_node(self):
        payload = {
            "nodes": ["A", "B", "A"],
            "edges": [
                {"id": "E1", "source": "A", "target": "B", "seconds": 1, "stairs": False},
                {"id": "E1", "source": "A", "target": "B", "seconds": 1, "stairs": False},
            ],
        }
        with self.assertRaises(db.ValidationError) as ctx:
            db.validate_graph(payload)
        self.assertIn("重复", str(ctx.exception))

    def test_closure_validation(self):
        good = {"closures": [
            {"edge_id": "E1", "start": "2026-10-05T08:00:00",
             "end": "2026-10-05T09:00:00"},
        ]}
        rows, aware = db.validate_closures(good, {"E1", "E2"})
        self.assertEqual(len(rows), 1)
        self.assertFalse(aware)

        # 未知路段
        bad = {"closures": [
            {"edge_id": "E9", "start": "2026-10-05T08:00:00",
             "end": "2026-10-05T09:00:00"},
        ]}
        with self.assertRaises(db.ValidationError):
            db.validate_closures(bad, {"E1"})

        # 开始不早于结束
        bad2 = {"closures": [
            {"edge_id": "E1", "start": "2026-10-05T09:00:00",
             "end": "2026-10-05T09:00:00"},
        ]}
        with self.assertRaises(db.ValidationError):
            db.validate_closures(bad2, {"E1"})

        # 时区混用
        bad3 = {"closures": [
            {"edge_id": "E1", "start": "2026-10-05T08:00:00",
             "end": "2026-10-05T09:00:00"},
            {"edge_id": "E1", "start": "2026-10-05T10:00:00+08:00",
             "end": "2026-10-05T11:00:00+08:00"},
        ]}
        with self.assertRaises(db.ValidationError):
            db.validate_closures(bad3, {"E1"})


class VersionPublishingTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_failed_import_keeps_old_version(self):
        r1 = db.publish_graph(self.conn, VALID_GRAPH)
        self.assertEqual(r1["graph_version"], 1)

        bad_graph = {
            "nodes": ["A"],
            "edges": [{"id": "X", "source": "A", "target": "B",
                       "seconds": 10, "stairs": False}],
        }
        with self.assertRaises(db.ValidationError):
            db.publish_graph(self.conn, bad_graph)

        snap = db.load_snapshot(self.conn)
        self.assertEqual(snap["graph_version"], 1)
        self.assertEqual(set(snap["nodes"]), {"A", "B", "C"})
        self.assertEqual(len(snap["edges"]), 2)

    def test_graph_and_closure_versions(self):
        db.publish_graph(self.conn, VALID_GRAPH)
        r = db.publish_closures(self.conn, {"closures": [
            {"edge_id": "E1", "start": "2026-10-05T08:00:00",
             "end": "2026-10-05T09:00:00"},
        ]})
        self.assertEqual(r["closure_version"], 1)
        snap = db.load_snapshot(self.conn)
        self.assertEqual(len(snap["closures"]), 1)

        # 新封闭版本替换指针，旧记录仍在库中
        db.publish_closures(self.conn, {"closures": [
            {"edge_id": "E2", "start": "2026-10-05T10:00:00",
             "end": "2026-10-05T11:00:00"},
        ]})
        snap2 = db.load_snapshot(self.conn)
        self.assertEqual(snap2["closure_version"], 2)
        self.assertEqual(snap2["closures"][0]["edge_id"], "E2")
        old = self.conn.execute(
            "SELECT COUNT(*) AS c FROM closures WHERE closure_version = 1"
        ).fetchone()
        self.assertEqual(old["c"], 1)

    def test_new_graph_clears_closure_pointer(self):
        db.publish_graph(self.conn, VALID_GRAPH)
        db.publish_closures(self.conn, {"closures": [
            {"edge_id": "E1", "start": "2026-10-05T08:00:00",
             "end": "2026-10-05T09:00:00"},
        ]})
        db.publish_graph(self.conn, VALID_GRAPH)
        cur = db.get_current_versions(self.conn)
        self.assertEqual(cur["graph_version"], 2)
        self.assertIsNone(cur["closure_version"])

    def test_closures_without_graph_rejected(self):
        with self.assertRaises(db.ValidationError):
            db.publish_closures(self.conn, {"closures": []})


if __name__ == "__main__":
    unittest.main()
