"""HTTP API 端到端测试（Flask test client + 临时 SQLite）。"""

import os
import tempfile
import unittest

from app.server import create_app


GRAPH = {
    "nodes": ["GATE", "PLAZA", "LAKE", "PAVILION", "EXIT"],
    "edges": [
        {"id": "E1", "source": "GATE", "target": "PLAZA", "seconds": 300, "stairs": False},
        {"id": "E2", "source": "PLAZA", "target": "LAKE", "seconds": 420, "stairs": False},
        {"id": "E3", "source": "PLAZA", "target": "PAVILION", "seconds": 240, "stairs": True},
        {"id": "E4", "source": "LAKE", "target": "PAVILION", "seconds": 300, "stairs": False},
        {"id": "E5", "source": "PAVILION", "target": "EXIT", "seconds": 180, "stairs": False},
        {"id": "E6", "source": "LAKE", "target": "EXIT", "seconds": 1200, "stairs": False},
    ],
}


class ApiTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config["DB_CONN"].close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_health(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_full_flow(self):
        # 1. 导入图
        resp = self.client.put("/api/graph", json=GRAPH)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.get_json()["graph_version"], 1)

        # 2. 导入封闭：E5 在 9:15-9:40 封闭；E3 在 14:00-15:00 封闭
        resp = self.client.put("/api/closures", json={"closures": [
            {"edge_id": "E5", "start": "2026-10-05T09:15:00",
             "end": "2026-10-05T09:40:00"},
            {"edge_id": "E3", "start": "2026-10-05T14:00:00",
             "end": "2026-10-05T15:00:00"},
        ]})
        self.assertEqual(resp.status_code, 200, resp.data)

        # 3. 查询：9:00 从 GATE 到 EXIT，台阶近路 E1+E3 于 9:09 到
        #    PAVILION，E5 通行区间 [9:09,9:12) 早于 9:15 的封闭，直接通过
        resp = self.client.post("/api/route", json={
            "origin": "GATE", "destination": "EXIT",
            "depart_at": "2026-10-05T09:00:00",
        })
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["edge_path"], ["E1", "E3", "E5"])
        self.assertEqual(body["arrive_at"], "2026-10-05T09:12:00")
        self.assertEqual([leg["waited_seconds"] for leg in body["legs"]],
                         [0, 0, 0])

        # 4. 9:12 出发且避台阶：绕行湖边 9:29 到 PAVILION，E5 在封闭中，
        #    等候至 9:40 进入，9:43 到达
        resp = self.client.post("/api/route", json={
            "origin": "GATE", "destination": "EXIT",
            "depart_at": "2026-10-05T09:12:00",
            "avoid_stairs": True,
        })
        body = resp.get_json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["edge_path"], ["E1", "E2", "E4", "E5"])
        self.assertEqual(body["arrive_at"], "2026-10-05T09:43:00")
        last = body["legs"][-1]
        self.assertEqual(last["enter_at"], "2026-10-05T09:40:00")
        self.assertEqual(last["waited_seconds"], 660)

        # 5. 14:00 出发：台阶路封闭要等到 15:00，绕湖路 14:20 到，选绕行
        resp = self.client.post("/api/route", json={
            "origin": "GATE", "destination": "EXIT",
            "depart_at": "2026-10-05T14:00:00",
        })
        body = resp.get_json()
        self.assertEqual(body["edge_path"], ["E1", "E2", "E4", "E5"])
        self.assertEqual(body["arrive_at"], "2026-10-05T14:20:00")

    def test_unknown_node(self):
        self.client.put("/api/graph", json=GRAPH)
        resp = self.client.post("/api/route", json={
            "origin": "MARS", "destination": "EXIT",
            "depart_at": "2026-10-05T09:00:00",
        })
        self.assertEqual(resp.status_code, 404)
        self.assertIn("MARS", resp.get_json()["error"])

    def test_no_route(self):
        # 只有一条台阶路，避台阶 -> 无可行路线
        graph = {"nodes": ["A", "B"], "edges": [
            {"id": "E1", "source": "A", "target": "B", "seconds": 10, "stairs": True},
        ]}
        self.client.put("/api/graph", json=graph)
        resp = self.client.post("/api/route", json={
            "origin": "A", "destination": "B",
            "depart_at": "2026-10-05T09:00:00",
            "avoid_stairs": True,
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["feasible"])
        self.assertIn("无可行路线", body["message"])

    def test_bad_import_rejected_and_old_version_kept(self):
        self.client.put("/api/graph", json=GRAPH)
        resp = self.client.put("/api/graph", json={
            "nodes": ["A"],
            "edges": [{"id": "E1", "source": "A", "target": "B",
                       "seconds": -1, "stairs": False}],
        })
        self.assertEqual(resp.status_code, 422)
        resp = self.client.get("/api/versions")
        self.assertEqual(resp.get_json()["graph_version"], 1)

    def test_route_before_graph(self):
        resp = self.client.post("/api/route", json={
            "origin": "A", "destination": "B",
            "depart_at": "2026-10-05T09:00:00",
        })
        self.assertEqual(resp.status_code, 409)

    def test_touching_closure_endpoint_allowed(self):
        graph = {"nodes": ["A", "B"], "edges": [
            {"id": "E1", "source": "A", "target": "B", "seconds": 600, "stairs": False},
        ]}
        self.client.put("/api/graph", json=graph)
        # 封闭在 9:10 结束；9:10 出发、9:20 离开，区间相触允许
        self.client.put("/api/closures", json={"closures": [
            {"edge_id": "E1", "start": "2026-10-05T08:00:00",
             "end": "2026-10-05T09:10:00"},
        ]})
        resp = self.client.post("/api/route", json={
            "origin": "A", "destination": "B",
            "depart_at": "2026-10-05T09:10:00",
        })
        body = resp.get_json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["arrive_at"], "2026-10-05T09:20:00")

        # 9:09 出发（9:19 离开）与封闭重叠 -> 等至 9:10
        resp = self.client.post("/api/route", json={
            "origin": "A", "destination": "B",
            "depart_at": "2026-10-05T09:09:00",
        })
        body = resp.get_json()
        self.assertEqual(body["legs"][0]["enter_at"], "2026-10-05T09:10:00")
        self.assertEqual(body["legs"][0]["waited_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
