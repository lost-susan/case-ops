"""Deterministic deadline arithmetic. Pure calendar math on dates: no clocks, locales or DST involved.
The legal RULES (days/months per citation) are data, stored per deadline and flagged unverified until
counsel confirms them. This module only does the arithmetic."""
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import utcnow


@dataclass(frozen=True)
class DeadlineResult:
    due: date
    days_left: int
    urgency: str
    formula: str


def add_months(start: date, months: int) -> date:
    index = start.year * 12 + (start.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))  # clamp: Aug 31 + 6mo = Feb 28


def urgency(days_left: int) -> str:
    if days_left < 0:
        return "Overdue"
    if days_left <= 7:
        return "Urgent"
    if days_left <= 30:
        return "Active"
    return "Upcoming"


def compute(rule_days: int | None, rule_months: int | None, trigger: date, today: date) -> DeadlineResult:
    if (rule_days is None) == (rule_months is None):
        raise ValueError("Exactly one of rule_days or rule_months is required")
    if rule_months is not None:
        due, unit = add_months(trigger, rule_months), f"{rule_months} months"
    else:
        due, unit = trigger + timedelta(days=rule_days), f"{rule_days} days"
    left = (due - today).days
    return DeadlineResult(due, left, urgency(left), f"{trigger.isoformat()} + {unit} = {due.isoformat()}")


def court_today(tz_name: str, now: datetime | None = None) -> date:
    """Today's calendar date in the court's timezone (not the server's or the viewer's)."""
    return (now or utcnow()).astimezone(ZoneInfo(tz_name)).date()
