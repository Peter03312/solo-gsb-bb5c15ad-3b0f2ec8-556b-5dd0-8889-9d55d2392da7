"""路由算法单元测试：封闭半开区间、节点等候、避台阶、字典序 tie-break。"""

import unittest
from datetime import datetime, timedelta

from app.routing import Closure, Edge, Graph, earliest_departure, find_route


def t(h, m=0):
    return datetime(2026, 10, 5, h, m, 0)


def make_graph(edge_specs, nodes=None):
    adj = {}
    node_set = set(nodes or [])
    for eid, s, dst, sec, stairs in edge_specs:
        node_set.update((s, dst))
        edge = Edge(eid, s, dst, sec, stairs)
        adj.setdefault(s, []).append(edge)
    for n in node_set:
        adj.setdefault(n, [])
    return Graph(nodes=node_set, adjacency=adj)


class EarliestDepartureTests(unittest.TestCase):
    def test_no_closure(self):
        self.assertEqual(earliest_departure(t(9), 100, []), t(9))

    def test_finish_exactly_when_closure_starts_is_allowed(self):
        # 半开：9:00 出发、10 分钟通行，封闭 9:10 开始 -> 端点相触允许
        self.assertEqual(
            earliest_departure(t(9), 600, [(t(9, 10), t(10))]), t(9)
        )

    def test_start_exactly_when_closure_ends_is_allowed(self):
        self.assertEqual(
            earliest_departure(t(10), 600, [(t(9), t(10))]), t(10)
        )

    def test_wait_for_closure_end(self):
        # 9:05 出发需 10 分钟，封闭 [9:00,9:30) -> 等至 9:30
        self.assertEqual(
            earliest_departure(t(9, 5), 600, [(t(9), t(9, 30))]), t(9, 30)
        )

    def test_skip_multiple_closures(self):
        windows = [(t(9), t(9, 30)), (t(9, 30), t(10))]
        self.assertEqual(
            earliest_departure(t(9, 5), 600, windows), t(10)
        )

    def test_closure_after_finish(self):
        self.assertEqual(
            earliest_departure(t(8), 600, [(t(9), t(10))]), t(8)
        )


class FindRouteTests(unittest.TestCase):
    def build(self):
        # A -> B -> D 与 A -> C -> D 两条路
        return make_graph([
            ("E1", "A", "B", 100, False),
            ("E2", "B", "D", 100, False),
            ("E3", "A", "C", 100, False),
            ("E4", "C", "D", 100, False),
        ])

    def test_lexicographic_tie_break(self):
        graph = self.build()
        result = find_route(graph, [], "A", "D", t(9), False)
        arrival, path, _ = result
        self.assertEqual(path, ("E1", "E2"))  # 同为 9:03:20，取字典序小
        self.assertEqual(arrival, t(9) + timedelta(seconds=200))

    def test_closure_forces_wait_and_other_path(self):
        graph = self.build()
        # 短路 E1 被封到 10:00 -> 选 E3,E4 不等待
        closures = [Closure("E1", t(8), t(10))]
        arrival, path, schedule = find_route(
            graph, closures, "A", "D", t(9), False
        )
        self.assertEqual(path, ("E3", "E4"))
        self.assertEqual(arrival, t(9) + timedelta(seconds=200))

    def test_wait_at_node_for_edge_closure(self):
        graph = make_graph([
            ("E1", "A", "B", 600, False),   # 9:00 -> 9:10
            ("E2", "B", "D", 600, False),   # 封闭 9:00-9:30 -> 9:30 -> 9:40
        ])
        closures = [Closure("E2", t(9), t(9, 30))]
        arrival, path, schedule = find_route(
            graph, closures, "A", "D", t(9), False
        )
        self.assertEqual([s[0].edge_id for s in schedule], ["E1", "E2"])
        self.assertEqual(schedule[0][1], t(9))
        self.assertEqual(schedule[0][2], t(9, 10))
        self.assertEqual(schedule[1][1], t(9, 30))  # 在 B 等候 20 分钟
        self.assertEqual(arrival, t(9, 40))

    def test_traversal_cannot_straddle_closure(self):
        # E1 9:00 出发通行 20 分钟，封闭 9:10-9:20：9:00 不可，等 9:20
        graph = make_graph([("E1", "A", "B", 1200, False)])
        closures = [Closure("E1", t(9, 10), t(9, 20))]
        arrival, path, schedule = find_route(
            graph, closures, "A", "B", t(9), False
        )
        self.assertEqual(schedule[0][1], t(9, 20))
        self.assertEqual(arrival, t(9, 40))

    def test_avoid_stairs(self):
        graph = make_graph([
            ("E1", "A", "B", 10, True),
            ("E2", "A", "C", 20, False),
            ("E3", "C", "B", 20, False),
        ])
        result = find_route(graph, [], "A", "B", t(9), True)
        _, path, _ = result
        self.assertEqual(path, ("E2", "E3"))
        # 不避台阶时选直达短路
        _, path2, _ = find_route(graph, [], "A", "B", t(9), False)
        self.assertEqual(path2, ("E1",))

    def test_no_route(self):
        # 避台阶后唯一含台阶的边不可用
        g2 = make_graph([("E1", "A", "B", 10, True)])
        self.assertIsNone(find_route(g2, [], "A", "B", t(9), True))
        # 根本不连通
        g3 = make_graph([("E1", "A", "B", 10, False)], nodes=["A", "B", "D"])
        self.assertIsNone(find_route(g3, [], "A", "D", t(9), False))

    def test_same_origin_destination(self):
        graph = self.build()
        arrival, path, schedule = find_route(
            graph, [], "A", "A", t(9), False
        )
        self.assertEqual(arrival, t(9))
        self.assertEqual(path, ())

    def test_unknown_node_raises(self):
        graph = self.build()
        with self.assertRaises(KeyError):
            find_route(graph, [], "A", "Z", t(9), False)


if __name__ == "__main__":
    unittest.main()
