import re

INT32_MAX = 2_147_483_647

# 8 integer digits, a point and 2 decimals is the longest amount that fits INT32_MAX cents.
_MAX_AMOUNT_LENGTH = 11
# [0-9], not \d: \d also matches digits from other scripts.
_AMOUNT_PATTERN = re.compile(r"[0-9]+(\.[0-9]{1,2})?")


def parse_amount_cents(value: str) -> int:
    """Convert a decimal amount string such as "5800.5" to integer cents."""
    if len(value) > _MAX_AMOUNT_LENGTH:
        raise ValueError(f"amount must be at most {_MAX_AMOUNT_LENGTH} characters long")
    if _AMOUNT_PATTERN.fullmatch(value) is None:
        raise ValueError("amount must be digits with an optional point and 1 or 2 decimals")
    whole, _, fraction = value.partition(".")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 0 < cents <= INT32_MAX:
        raise ValueError(f"amount must be greater than 0 and at most {format_cents(INT32_MAX)}")
    return cents


def format_cents(cents: int) -> str:
    """Render non-negative integer cents as a decimal string with exactly two decimals."""
    return f"{cents // 100}.{cents % 100:02d}"
