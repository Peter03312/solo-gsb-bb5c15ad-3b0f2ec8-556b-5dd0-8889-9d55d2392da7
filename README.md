# 园区访客离线通行规划服务

面向园区访客的**离线**通行规划 HTTP 服务：导入带有方向、通行秒数、台阶标记的步道图
与按半开时间区间生效的封闭记录，根据起终点、出发时刻与是否避台阶，返回**最早到达**
的路线及每段路的进入、离开时刻与在节点处的等候时间。

- 语言/运行时：Python 3.11
- 存储：SQLite（WAL 模式，单文件，随容器卷持久化）
- Web 框架：Flask（开发）+ waitress（容器内生产启动）
- 无需任何外部网络/服务，适合内网或离线环境运行

## 目录结构

```
app/
  timeutil.py    # ISO 8601 时间解析、时区一致性校验
  routing.py     # 最早到达 Dijkstra（半开区间避让、节点等候、字典序 tie-break）
  db.py          # SQLite 版本化存储、导入校验、原子发布、版本快照读取
  server.py      # HTTP API（Flask）
  init_data.py   # 首次启动导入种子数据
seed/            # 随附的示例步道图与封闭记录
wsgi.py          # 应用入口（导入时自动初始化数据）
start.sh         # 容器启动脚本（初始化 + waitress）
tests/           # 单元 / 端到端测试（标准库 unittest）
Dockerfile
docker-compose.yml
requirements.txt
```

## 一键启动（推荐）

```bash
docker compose up --build
```

服务地址与端口：`http://localhost:8080`

数据保存在名为 `park-data` 的 Docker 卷内（`/data/park.db`）。
首次启动若数据库为空，会自动导入 `seed/` 下的示例步道图与封闭记录。

停止：`docker compose down`（加 `-v` 可同时删除数据卷，恢复初始种子数据）。

## 本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选：配置（括号内为默认值）
export PARK_DB_PATH=./data/park.db          # SQLite 数据库文件路径
export PARK_HOST=0.0.0.0                    # 监听地址
export PARK_PORT=8080                       # 监听端口
export PARK_SEED_GRAPH=seed/graph.json      # 初始步道图（skip 表示不导入）
export PARK_SEED_CLOSURES=seed/closures.json# 初始封闭记录（skip 表示不导入）

python wsgi.py        # 首次运行自动建库并导入种子数据
```

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PARK_DB_PATH` | `/data/park.db` | SQLite 数据库文件路径，目录不存在会自动创建 |
| `PARK_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PARK_PORT` | `8080` | HTTP 监听端口 |
| `PARK_THREADS` | `8` | waitress 工作线程数 |
| `PARK_SEED_GRAPH` | `/app/seed/graph.json` | 启动时库为空则导入的步道图 JSON，`skip` 跳过 |
| `PARK_SEED_CLOSURES` | `/app/seed/closures.json` | 启动时库为空则导入的封闭 JSON，`skip` 跳过 |
| `PARK_DEBUG` | `0` | 本地开发置 `1` 开启 Flask debug |

> 服务端口固定由 `PARK_PORT` 控制；Compose 默认把宿主机 `8080` 映射到容器 `8080`。

## HTTP API

所有接口均使用 JSON。时间为 ISO 8601，可带或不带时区（如 `2026-10-05T09:00:00`
或 `2026-10-05T09:00:00+08:00`，也支持末尾 `Z`）。**同一批封闭记录与查询的出发时间
必须统一带时区或统一不带**，不允许混用。

### 1. 健康检查

```
GET /health
```

### 2. 导入步道图（原子发布新版本）

```
PUT /api/graph
```

请求体：

```json
{
  "nodes": ["GATE", "PLAZA", "LAKE", "PAVILION", "EXIT"],
  "edges": [
    {"id": "E1", "source": "GATE",  "target": "PLAZA",    "seconds": 300, "stairs": false},
    {"id": "E3", "source": "PLAZA", "target": "PAVILION", "seconds": 240, "stairs": true}
  ]
}
```

- 图是**有向图**；`seconds` 为正整数通行秒数；`stairs` 为布尔台阶标记。
- 节点也可写成 `{"id": "GATE"}`；编号（节点/路段）支持字符串或整数（内部按字符串比较）。
- 引用未知节点、非正通行秒数、编号重复等导入会被**整体拒绝（HTTP 422），保留旧版本**。
- 发布新图后封闭版本指针会清空（旧封闭针对的是旧图），请随后重新导入封闭记录。
- 成功返回新版本号，例如：
  `{"ok": true, "graph_version": 2, "node_count": 5, "edge_count": 6}`

### 3. 导入封闭记录（原子发布新版本）

```
PUT /api/closures
```

请求体：

```json
{
  "closures": [
    {"edge_id": "E5", "start": "2026-10-05T09:15:00", "end": "2026-10-05T09:40:00"},
    {"edge_id": "E3", "start": "2026-10-05T14:00:00", "end": "2026-10-05T15:00:00"}
  ]
}
```

- 封闭按**半开区间 `[start, end)`** 生效；`start` 必须严格早于 `end`。
- `edge_id` 必须存在于**当前发布的图**；区间非法、引用不存在路段、时区混用时
  返回 **HTTP 422 且保留旧封闭版本**。
- 成功：`{"ok": true, "closure_version": 2, "closure_count": 2, "graph_version": 2}`

### 4. 查询路线

```
POST /api/route
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `origin` | string | 是 | 起点节点编号 |
| `destination` | string | 是 | 终点节点编号 |
| `depart_at` | string | 是 | ISO 8601 出发时刻 |
| `avoid_stairs` | bool | 否 | 默认 `false`，为 `true` 时不使用台阶路段 |

响应（有可行路线，HTTP 200）：

```json
{
  "ok": true,
  "feasible": true,
  "origin": "GATE",
  "destination": "EXIT",
  "depart_at": "2026-10-05T09:12:00",
  "arrive_at": "2026-10-05T09:43:00",
  "total_seconds": 1860,
  "avoid_stairs": true,
  "edge_path": ["E1", "E2", "E4", "E5"],
  "legs": [
    {"edge_id": "E1", "from_node": "GATE", "to_node": "PLAZA",
     "seconds": 300, "stairs": false,
     "enter_at": "2026-10-05T09:12:00", "leave_at": "2026-10-05T09:17:00",
     "waited_seconds": 0},
    {"edge_id": "E5", "from_node": "PAVILION", "to_node": "EXIT",
     "seconds": 180, "stairs": false,
     "enter_at": "2026-10-05T09:40:00", "leave_at": "2026-10-05T09:43:00",
     "waited_seconds": 660}
  ],
  "graph_version": 2,
  "closure_version": 2
}
```

- `waited_seconds` 为进入该路段前在节点上的等候秒数（首段相对出发时刻）。
- 允许在节点等候封闭结束；但某路段的**整个通行区间 `[enter, leave)` 不得与该路段的
  任一封闭区间重叠**。半开区间端点相触允许（在封闭结束瞬间进入、或在封闭开始瞬间离开）。
- 路线选择：首先最早到达；同一到达时刻按**路段编号序列字典序**最小者。
- 响应中的 `graph_version` / `closure_version` 标明本次查询实际使用的版本——
  单次查询只会使用同一个版本，即便期间发生了新的导入。

无可行路线（HTTP 200，业务上明确告知）：

```json
{"ok": true, "feasible": false, "message": "在给定出发时刻与避台阶条件下无可行路线", ...}
```

其他错误：起点/终点未知 → `404`；尚未发布图 → `409`；请求参数/时间格式错误 → `400`。

### 5. 查看当前生效版本

```
GET /api/versions
```

返回 `{"ok": true, "graph_version": 2, "closure_version": 2}`。

## 调用示例

以下示例使用随服务启动自动导入的种子数据（5 个节点、6 条有向路段；
E5 在 09:15–09:40 封闭，E3 在 14:00–15:00 封闭）。

```bash
# 09:00 出发：台阶近路 09:12 到达，E5 在 [09:09,09:12) 之后才封闭，直接通过
curl -s http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"EXIT","depart_at":"2026-10-05T09:00:00"}'

# 09:12 出发并避台阶：绕行湖边，09:29 到 PAVILION 时 E5 仍在封闭，
# 在节点等候 11 分钟至 09:40 进入，09:43 到达
curl -s http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"EXIT",
       "depart_at":"2026-10-05T09:12:00","avoid_stairs":true}'

# 14:00 出发：台阶路 E3 封闭至 15:00，系统改选绕湖路线 14:20 到达
curl -s http://localhost:8080/api/route \
  -H 'Content-Type: application/json' \
  -d '{"origin":"GATE","destination":"EXIT","depart_at":"2026-10-05T14:00:00"}'

# 更新步道图 / 封闭记录（原子发布新版本；非法数据返回 422 且不影响线上版本）
curl -s -X PUT http://localhost:8080/api/graph \
  -H 'Content-Type: application/json' --data @seed/graph.json
curl -s -X PUT http://localhost:8080/api/closures \
  -H 'Content-Type: application/json' --data @seed/closures.json
```

## 数据模型与版本一致性

- 步道图与封闭记录各自维护不可变的版本表，另有单行指针表记录当前生效版本号。
- 每次导入：先做完整校验 → 在一个 SQLite 事务（`BEGIN IMMEDIATE`）内写入新版本并
  切换指针 → 提交。任何校验错误都发生在写入之前，**旧版本继续可用**。
- 查询时先读取指针中的版本号，再显式按该版本号读取全部节点、路段与封闭记录，
  保证单次查询看到的是同一个自洽版本（快照语义）。
- 旧版本数据不会被删除，可随时审计；服务重启后指针与历史版本均保留。

## 运行测试

```bash
python -m unittest discover -s tests -v
```

覆盖：半开区间端点规则、整段通行区间不得重叠、节点等候、避台阶、字典序 tie-break、
无路线告知、未知节点 / 非正秒数 / 无效区间拒绝且保留旧版本、版本指针切换、
HTTP 端到端及并发读写一致性。
