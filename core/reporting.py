"""Police reporting periods; independent of rounded billing month counts."""
import re
from datetime import date, timedelta

from django.core.exceptions import ValidationError

from .date_utils import add_months

DATE_RE = re.compile(r"(?<!\d)(\d{4})[./年-](\d{1,2})[./月-](\d{1,2})(?:日)?(?!\d)")


def shorter_than_months(start, end, months):
    return end < add_months(start, months) - timedelta(days=1)


def text_period(text):
    try:
        dates = [date(*map(int, match.groups())) for match in DATE_RE.finditer(text or "")]
    except ValueError as exc:
        raise ValidationError("报备内容包含不存在的日期，请核对。") from exc
    if len(dates) >= 2 and dates[1] < dates[0]:
        raise ValidationError("报备结束日期不能早于开始日期。")
    return dates[:2]


def replace_text_period(text, start, end):
    """Replace a personal period while retaining its explanatory text."""
    period = f"{start:%Y.%m.%d}-{end:%Y.%m.%d}"
    matches = list(DATE_RE.finditer(text or ""))
    if len(matches) >= 2:
        return text[:matches[0].start()] + period + text[matches[1].end():]
    return f"{period} {text}".strip()


def validate_report_period(start, end):
    if start and end and end < start:
        raise ValidationError("报备结束日期不能早于开始日期。")
