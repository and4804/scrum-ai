"""Shim: existing modules import `telegram_utils`. Prefer `whatsapp_utils` in new code."""

from whatsapp_utils import *  # noqa: F403
