"""Inline keyboards and the callback payloads they carry."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import SETTINGS_CHOICES, OutputFormat


class OutputCallback(CallbackData, prefix="out"):
    """Which format to deliver for the image attached to this message."""

    fmt: OutputFormat


class SettingsCallback(CallbackData, prefix="cfg"):
    """Which default output format to persist for this user."""

    fmt: OutputFormat


def output_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fmt in (OutputFormat.STICKER, OutputFormat.PNG, OutputFormat.BOTH):
        builder.button(text=fmt.label, callback_data=OutputCallback(fmt=fmt))
    builder.adjust(1)
    return builder.as_markup()


def settings_keyboard(current: OutputFormat) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for fmt in SETTINGS_CHOICES:
        marker = "✅ " if fmt is current else ""
        builder.button(text=f"{marker}{fmt.label}", callback_data=SettingsCallback(fmt=fmt))
    builder.adjust(1)
    return builder.as_markup()
