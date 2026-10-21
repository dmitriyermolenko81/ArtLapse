"""Consistent timeline time labels."""


def format_time(seconds: float, precision: int = 0) -> str:
    """Format elapsed time as MM:SS or HH:MM:SS, with optional decimals."""
    if precision < 0:
        raise ValueError("precision must be non-negative")
    scale = 10**precision
    units = max(0, round(seconds * scale))
    whole_seconds, fraction = divmod(units, scale)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, second = divmod(remainder, 60)
    text = (
        f"{hours:02d}:{minutes:02d}:{second:02d}"
        if hours
        else f"{minutes:02d}:{second:02d}"
    )
    if precision:
        text += f".{fraction:0{precision}d}"
    return text
