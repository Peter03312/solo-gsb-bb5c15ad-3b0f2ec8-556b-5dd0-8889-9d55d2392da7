"""时间解析与比较工具。

支持 ISO 8601 形式（如 ``2026-10-05T09:00:00`` 或
``2026-10-05T09:00:00+08:00``，``Z`` 结尾视作 UTC）。

约定：同一次导入 / 同一次查询中的时间要么全部带时区，要么全部不带时区，
不允许混用，以避免隐式时区假设。
"""

from __future__ import annotations

from datetime import datetime


def parse_datetime(value: object, field: str = "time") -> datetime:
    """解析时间字符串。

    成功返回 aware 或 naive 的 ``datetime``；格式不合法时抛出
    :class:`ValueError`，错误信息以中文说明字段名。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是 ISO 8601 时间字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间: {value!r}")
    return dt


def is_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def same_awareness(a: datetime, b: datetime) -> bool:
    return is_aware(a) == is_aware(b)


def canonical(dt: datetime) -> str:
    """以规范 ISO 8601 字符串落库。"""
    return dt.isoformat()


def format_dt(dt: datetime) -> str:
    """对外输出（与落库形式一致，保留原始时区信息）。"""
    return dt.isoformat()
