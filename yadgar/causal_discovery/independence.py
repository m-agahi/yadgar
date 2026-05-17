"""Conditional independence tests for the PC algorithm."""

import logging
import math

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)


def conditional_independence_test(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray | None = None,
    alpha: float = 0.05,
) -> bool:
    """Test if X is independent of Y given Z.

    Returns True if independent (p_value > alpha), False if dependent.
    """
    n = len(x)
    if n < 4:
        return True  # Not enough data to determine dependence

    if z is None:
        # Unconditional: Pearson correlation test
        if np.std(x) < 1e-10 or np.std(y) < 1e-10:
            return True  # constant variable -> independence ill-defined
        r = np.corrcoef(x, y)[0, 1]
        if np.isnan(r):
            return True  # constant variable -> treat as independent
        denom = 1.0 - r * r
        if denom <= 0:
            return False  # perfect correlation -> dependent
        t_stat = r * math.sqrt((n - 2) / denom)
        p_value = 2.0 * stats.t.sf(abs(t_stat), df=n - 2)
    else:
        # Partial correlation: regress X on Z and Y on Z, correlate residuals
        if z.ndim == 1:
            z = z.reshape(-1, 1)

        # Add intercept column
        ones = np.ones((n, 1))
        Z = np.hstack([ones, z])

        # Compute residuals via least squares
        try:
            res_x = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
            res_y = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            return True  # Singular matrix -> treat as independent

        # Check for zero-variance residuals
        if np.std(res_x) < 1e-10 or np.std(res_y) < 1e-10:
            return True

        r = np.corrcoef(res_x, res_y)[0, 1]
        if np.isnan(r):
            return True

        dof = n - 2 - z.shape[1]
        if dof < 1:
            return True  # Not enough degrees of freedom

        denom = 1.0 - r * r
        if denom <= 0:
            return False

        t_stat = r * math.sqrt(dof / denom)
        p_value = 2.0 * stats.t.sf(abs(t_stat), df=dof)

    return bool(p_value > alpha)
