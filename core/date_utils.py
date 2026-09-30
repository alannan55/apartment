import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP


MONEY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def days_in_month(value: date) -> int:
    return calendar.monthrange(value.year, value.month)[1]


def contract_months(start: date, end: date) -> int:
    months = 0
    cursor = start
    limit = end + timedelta(days=1)
    while add_months(start, months + 1) <= limit:
        months += 1
        cursor = add_months(start, months)
    if cursor < limit:
        months += 1
    return max(months, 1)


def prorate_by_month(monthly_amount: Decimal, start: date, end: date) -> Decimal:
    if start > end:
        return Decimal("0.00")
    days = (end - start).days + 1
    return money(Decimal(monthly_amount) * Decimal(days) / Decimal(days_in_month(start)))


def long_term_first_rent(monthly_amount: Decimal, start: date) -> Decimal:
    if start.day == 1:
        return money(monthly_amount)
    billable_days = days_in_month(start) - start.day
    return money(Decimal(monthly_amount) * Decimal(billable_days) / Decimal(days_in_month(start)))


def is_heating_day(value: date) -> bool:
    return (value.month == 11 and value.day >= 15) or value.month in {12, 1, 2} or (
        value.month == 3 and value.day <= 15
    )
