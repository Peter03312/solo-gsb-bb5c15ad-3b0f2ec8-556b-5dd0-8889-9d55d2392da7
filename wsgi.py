"""WSGI 入口：初始化数据并启动 HTTP 服务。

本地开发：``python wsgi.py``（Flask 内置服务器）
容器内由 ``start.sh`` 使用 ``waitress-serve`` 启动。
"""

from __future__ import annotations

import os

from app.init_data import seed_if_empty
from app.server import create_app

db_path = os.environ.get("PARK_DB_PATH", "/data/park.db")
app = create_app(db_path)

# 启动时确保初始数据就绪（仅在数据库为空时导入）
seed_if_empty(app.config["DB_CONN"])

if __name__ == "__main__":
    host = os.environ.get("PARK_HOST", "0.0.0.0")
    port = int(os.environ.get("PARK_PORT", "8080"))
    debug = os.environ.get("PARK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
