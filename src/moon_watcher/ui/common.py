"""Shared bits for the screens. A screen is a plain function returning (text, keyboard)."""

from datetime import datetime, timezone
from hashlib import sha1

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from moon_watcher.utils.fmt import fmt_elapsed

PAGE_SIZE = 8

Screen = tuple[str, InlineKeyboardMarkup]


def btn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    """style is primary / success / danger; older PTB versions don't know it."""
    kwargs = {"text": text, "callback_data": data}
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


def keyboard(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows if r])


def menu_btn() -> InlineKeyboardButton:
    return btn("🏠 Menu", "menu")


def back_btn(data: str) -> InlineKeyboardButton:
    return btn("◀️ Back", data)


def item_id(value: str) -> str:
    """Short hash instead of a list index, so old buttons still point to the right site after .env changes."""
    return sha1(value.encode()).hexdigest()[:10]


def find(values: list[str], iid: str) -> str | None:
    return next((v for v in values if item_id(v) == iid), None)


def pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}:{page + 1}"))
    return row


def page_arg(args: list[str], index: int = 0) -> int:
    try:
        return max(0, int(args[index]))
    except (IndexError, ValueError):
        return 0


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    return "just now" if seconds < 5 else f"{fmt_elapsed(seconds)} ago"


def utc(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "never"


def gone(back: str) -> Screen:
    return (
        "🤷 This item is no longer configured. Check your .env.",
        keyboard([back_btn(back), menu_btn()]),
    )
