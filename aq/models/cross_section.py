"""截面模型：LightGBM / XGBoost / 线性回归（自动降级）。

P2 阶段接入真实训练。当前提供可运行的 sklearn 兜底实现，
保证无 lightgbm/xgboost 时也能跑通全流程。
"""

from __future__ import annotations

import numpy as np

from aq.models.base import ModelBase


class CrossSectionModel(ModelBase):
    """横截面预测：输入因子矩阵，输出个股打分/收益率预测。"""

    name = "cross_section"

    def __init__(self, backend: str = "lightgbm", **params) -> None:
        self.backend = backend
        self.params = params
        self.model = None
        self._feature_names: list[str] = []

    # ------------------------------------------------------------------ fit
    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: list[str] | None = None, **kwargs) -> "CrossSectionModel":
        self._feature_names = feature_names or [f"f{i}" for i in range(X.shape[1])]
        backend = self.backend

        if backend == "lightgbm":
            try:
                import lightgbm as lgb

                self.model = lgb.LGBMRegressor(
                    n_estimators=self.params.get("n_estimators", 300),
                    learning_rate=self.params.get("learning_rate", 0.05),
                    num_leaves=self.params.get("num_leaves", 31),
                    verbose=-1,
                )
                self.model.fit(X, y)
                return self
            except ImportError:
                backend = "linear"

        if backend == "xgboost":
            try:
                import xgboost as xgb

                self.model = xgb.XGBRegressor(
                    n_estimators=self.params.get("n_estimators", 300),
                    learning_rate=self.params.get("learning_rate", 0.05),
                    verbosity=0,
                )
                self.model.fit(X, y)
                return self
            except ImportError:
                backend = "linear"

        # 兜底：岭回归
        from sklearn.linear_model import Ridge

        self.model = Ridge(alpha=self.params.get("alpha", 1.0))
        self.model.fit(X, y)
        self.backend = backend if backend != "lightgbm" else "linear"
        return self

    # -------------------------------------------------------------- predict
    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("模型尚未训练，请先调用 fit()")
        return np.asarray(self.model.predict(X)).ravel()

    def feature_importance(self) -> dict[str, float] | None:
        if self.model is None:
            return None
        if hasattr(self.model, "feature_importances_"):
            imp = self.model.feature_importances_
        elif hasattr(self.model, "coef_"):
            imp = np.abs(np.ravel(self.model.coef_))
        else:
            return None
        total = imp.sum() or 1.0
        return {n: float(v / total) for n, v in zip(self._feature_names, imp)}
