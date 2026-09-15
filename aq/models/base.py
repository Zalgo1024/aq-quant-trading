"""模型基类与注册表（MLOps 的基础）。

所有模型统一接口：``fit(X, y)`` / ``predict(X)`` / ``save(dir)`` / ``load(dir)``，
便于 MLflow 记录版本与指标。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np


class ModelBase(ABC):
    name: str = "base"
    version: str = "0.1.0"

    @abstractmethod
    def fit(self, X, y, **kwargs) -> "ModelBase": ...

    @abstractmethod
    def predict(self, X) -> np.ndarray: ...

    def save(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": self.name,
            "version": self.version,
            "saved_at": datetime.now().isoformat(),
        }
        (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def feature_importance(self) -> dict[str, float] | None:
        return None


class ModelRegistry:
    """极简模型注册表（P6 可替换为 MLflow）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_file = self.root / "index.json"
        self._index: list[dict[str, Any]] = (
            json.loads(self._index_file.read_text(encoding="utf-8"))
            if self._index_file.exists()
            else []
        )

    def register(self, model: ModelBase, metrics: dict[str, Any] | None = None) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        mid = f"{model.name}_{ts}"
        model.save(self.root / mid)
        self._index.append(
            {
                "model_id": mid,
                "name": model.name,
                "version": model.version,
                "metrics": metrics or {},
                "path": str(self.root / mid),
                "created_at": datetime.now().isoformat(),
            }
        )
        self._index_file.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return mid

    def list(self) -> list[dict[str, Any]]:
        return list(self._index)
