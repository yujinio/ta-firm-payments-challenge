import pytest

from app.schemas import format_cents, parse_amount_cents


@pytest.mark.parametrize(
    "text_, cents",
    [
        ("1200.75", 120_075),
        ("300", 30_000),
        ("5800.5", 580_050),
        ("0.01", 1),
        ("007", 700),
        ("21474836.47", 2_147_483_647),
    ],
)
def test_valid_amounts(text_, cents):
    assert parse_amount_cents(text_) == cents


@pytest.mark.parametrize(
    "text_",
    [
        "0",
        "0.00",
        "-1",
        "+1",
        "1.234",
        "1.",
        ".5",
        "1e3",
        " 1",
        "1 ",
        "1\n",
        "1,5",
        "",
        "NaN",
        "١٢",
        "21474836.48",
        "000000000001",  # 12 characters, although the value (100 cents) is in range
        "9" * 5000,
    ],
)
def test_invalid_amounts(text_: str) -> None:
    """Only ASCII digits, an optional point and 1 or 2 decimals, in range and 11 long at most."""
    with pytest.raises(ValueError):
        parse_amount_cents(text_)


@pytest.mark.parametrize(
    "cents, text_",
    [(625_000, "6250.00"), (580_050, "5800.50"), (1, "0.01"), (1_325_125, "13251.25")],
)
def test_format_cents(cents, text_):
    assert format_cents(cents) == text_
