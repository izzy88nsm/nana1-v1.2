# src/science_and_strategy/formulas.py

from typing import Sequence, Optional, Tuple
from collections import deque
import math
import statistics
from numbers import Real
from functools import lru_cache

def moving_average(
    data: Sequence[float],
    period: int,
    ma_type: str = 'simple',
    adjust: bool = False
) -> Optional[float]:
    if period < 1 or len(data) < period:
        return None

    if ma_type == 'simple':
        return statistics.mean(data[-period:])

    elif ma_type in ('exponential', 'wilders'):
        alpha = 1/period if ma_type == 'wilders' else 2/(period + 1)
        ema = data[-period]
        for price in reversed(data[-period+1:]):
            ema = price * alpha + ema * (1 - alpha)
        return ema if adjust else ema / (1 - (1 - alpha)**period)

    raise ValueError(f"Unsupported MA type: {ma_type}")

def rsi(
    data: Sequence[float],
    period: int = 14,
    ma_type: str = 'wilders'
) -> Optional[float]:
    if len(data) < period + 1:
        return None

    deltas = [data[i] - data[i-1] for i in range(1, period+1)]
    avg_gain = avg_loss = 0.0
    for delta in deltas:
        if delta > 0:
            avg_gain += delta
        else:
            avg_loss += abs(delta)
    avg_gain /= period
    avg_loss /= period

    for price in data[period+1:]:
        delta = price - data[data.index(price)-1]
        gain = max(delta, 0)
        loss = abs(min(delta, 0))

        if ma_type == 'wilders':
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        else:
            alpha = 1/period if ma_type == 'ema' else 2/(period + 1)
            avg_gain = gain * alpha + avg_gain * (1 - alpha)
            avg_loss = loss * alpha + avg_loss * (1 - alpha)

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
    ma_type: str = 'wilders'
) -> Optional[float]:
    if not all(len(seq) >= period + 1 for seq in (high, low, close)):
        return None

    tr_values = []
    prev_close = close[0]
    for h, l, c in zip(high[1:], low[1:], close[1:]):
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        tr_values.append(tr)
        prev_close = c

    return moving_average(tr_values, period, ma_type)

def z_score(
    value: float,
    data: Sequence[float],
    lookback: Optional[int] = None,
    ddof: int = 1
) -> Optional[float]:
    calc_data = data[-lookback:] if lookback else data
    if len(calc_data) < 2:
        return None
    try:
        mu = statistics.mean(calc_data)
        sigma = statistics.stdev(calc_data, xbar=mu, ddof=ddof)
        return (value - mu) / sigma if sigma != 0 else 0.0
    except statistics.StatisticsError:
        return None

def macd(
    data: Sequence[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9
) -> Optional[Tuple[float, float, float]]:
    if len(data) < slow_period + signal_period:
        return None

    fast_ema = moving_average(data, fast_period, 'exponential')
    slow_ema = moving_average(data, slow_period, 'exponential')
    if None in (fast_ema, slow_ema):
        return None

    macd_line = fast_ema - slow_ema
    signal_line = moving_average(
        [macd_line] * signal_period,
        signal_period,
        'exponential'
    )
    return (
        macd_line,
        signal_line,
        macd_line - signal_line if signal_line else 0.0
    )

def bollinger_bands(
    data: Sequence[float],
    period: int = 20,
    num_std: float = 2.0
) -> Optional[Tuple[float, float, float]]:
    if len(data) < period:
        return None

    ma = moving_average(data, period)
    std = statistics.stdev(data[-period:])
    return (
        ma,
        ma + num_std * std,
        ma - num_std * std
    )

@lru_cache(maxsize=128)
def fibonacci_retracement(
    high: float,
    low: float
) -> dict:
    diff = high - low
    return {
        '0.0%': high,
        '23.6%': high - diff * 0.236,
        '38.2%': high - diff * 0.382,
        '50.0%': high - diff * 0.5,
        '61.8%': high - diff * 0.618,
        '100.0%': low
    }

