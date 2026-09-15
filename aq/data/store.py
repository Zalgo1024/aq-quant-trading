"""本地存储：日线落盘为 parquet。

P0 先用 parquet（Zero 依赖、易迁移）；P1 起可切 PostgreSQL + TimescaleDB
（schema 见 ``sql/schema.sql``）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from aq.config.settings import PROJECT_ROOT
from aq.core.models import Bar


class BarStore:
    """按 symbol 存储日线的简单封装。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else PROJECT_ROOT / "data_cache" / "bars"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_of(self, symbol: str) -> Path:
        return self.root / f"{symbol}.parquet"

    def save(self, symbol: str, bars: list[Bar]) -> None:
        if not bars:
            return
        df = pd.DataFrame([b.model_dump(mode="json") for b in bars])
        df.to_parquet(self.path_of(symbol), index=False)

    def load(self, symbol: str) -> list[Bar]:
        p = self.path_of(symbol)
        if not p.exists():
            return []
        df = pd.read_parquet(p)
        return [Bar(**row) for row in df.to_dict(orient="records")]

    def exists(self, symbol: str) -> bool:
        return self.path_of(symbol).exists()
