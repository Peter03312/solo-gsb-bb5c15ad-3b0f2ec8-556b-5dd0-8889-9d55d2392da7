"""通行规划核心：时间相关的最短路（最早到达）算法。

规则
----
* 步道图是有向图，每条路段有正的通行秒数与台阶标记。
* 封闭记录按半开区间 ``[start, end)`` 生效：路段的整个通行区间
  ``[进入时刻, 离开时刻)`` 不得与该路段任意封闭区间相交；
  区间端点相触允许（离开时刻 == 封闭开始，或进入时刻 == 封闭结束）。
* 允许在节点等候封闭结束。
* 同一到达时刻，按“路段编号序列”的字典序选择路线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import heapq
from typing import Iterable

from .timeutil import parse_datetime, same_awareness


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source: str
    target: str
    seconds: int
    stairs: bool


@dataclass
class Graph:
    nodes: set[str]
    adjacency: dict[str, list[Edge]] = field(default_factory=dict)

    def outgoing(self, node: str) -> list[Edge]:
        return self.adjacency.get(node, [])


@dataclass(frozen=True)
class Closure:
    edge_id: str
    start: datetime
    end: datetime


def earliest_departure(
    ready_at: datetime,
    seconds: int,
    windows: Iterable[tuple[datetime, datetime]],
) -> datetime | None:
    """求最早可进入时刻 ``t >= ready_at``，使 ``[t, t+seconds)``
    与所有封闭半开区间都不重叠。

    算法：若与某个封闭区间冲突，则把进入时刻推迟到该区间结束，
    再从头检查；封闭区间数量有限且每次跳过一个区间，必然终止。
    """
    traverse = timedelta(seconds=seconds)
    t = ready_at
    for start, end in sorted(windows, key=lambda w: w[0]):
        # 半开区间相交判定：t < end 且 t+seconds > start
        if t < end and t + traverse > start:
            t = end
    return t


def find_route(
    graph: Graph,
    closures: list[Closure],
    origin: str,
    destination: str,
    depart_at: datetime,
    avoid_stairs: bool,
):
    """运行最早到达 Dijkstra。

    返回 ``(arrival, edge_path, schedule)``；不可达时返回 ``None``。
    ``edge_path`` 为路段编号元组，``schedule`` 为逐段
    ``(edge, enter_at, leave_at)`` 列表。

    标签比较键：``(到达时刻, 路段编号元组)``，天然实现“最早到达，
    同到达按编号序列字典序”。
    """
    if origin not in graph.nodes:
        raise KeyError(origin)
    if destination not in graph.nodes:
        raise KeyError(destination)

    closure_by_edge: dict[str, list[tuple[datetime, datetime]]] = {}
    for c in closures:
        if not same_awareness(c.start, depart_at):
            raise ValueError("封闭记录的时区性质（带/不带时区）必须与出发时刻一致")
        closure_by_edge.setdefault(c.edge_id, []).append((c.start, c.end))

    if origin == destination:
        return depart_at, (), []

    # 节点当前最优标签：到达时刻 + 路段序列 + 逐段时刻
    best_arrival: dict[str, datetime] = {origin: depart_at}
    best_path: dict[str, tuple[str, ...]] = {origin: ()}
    best_schedule: dict[str, list] = {origin: []}

    # 堆元素：(arrival, path_tuple, node, schedule)
    heap: list = [
        (depart_at, (), origin, [])
    ]

    while heap:
        arrival, path, node, schedule = heapq.heappop(heap)
        # 过期标签：节点已有更优标签则跳过
        cur_arrival = best_arrival.get(node)
        if cur_arrival is None or arrival != cur_arrival:
            continue
        if path != best_path.get(node):
            continue
        if node == destination:
            return arrival, path, schedule

        for edge in graph.outgoing(node):
            if avoid_stairs and edge.stairs:
                continue
            windows = closure_by_edge.get(edge.edge_id, [])
            enter_at = earliest_departure(arrival, edge.seconds, windows)
            leave_at = enter_at + timedelta(seconds=edge.seconds)
            new_path = path + (edge.edge_id,)
            new_schedule = schedule + [(edge, enter_at, leave_at)]

            old_arrival = best_arrival.get(edge.target)
            if (
                old_arrival is None
                or leave_at < old_arrival
                or (leave_at == old_arrival and new_path < best_path.get(edge.target))
            ):
                best_arrival[edge.target] = leave_at
                best_path[edge.target] = new_path
                best_schedule[edge.target] = new_schedule
                heapq.heappush(heap, (leave_at, new_path, edge.target, new_schedule))

    return None


def parse_closure_rows(rows: list[dict], depart_at: datetime) -> list[Closure]:
    """把数据库中存储的封闭记录（ISO 字符串）转为 :class:`Closure`。"""
    result: list[Closure] = []
    for row in rows:
        start = parse_datetime(row["start"], "封闭开始时间")
        end = parse_datetime(row["end"], "封闭结束时间")
        if not same_awareness(start, depart_at):
            raise ValueError("封闭记录的时区性质（带/不带时区）必须与出发时刻一致")
        result.append(Closure(edge_id=row["edge_id"], start=start, end=end))
    return result
