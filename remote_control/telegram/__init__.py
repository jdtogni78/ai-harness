"""Telegram inbound bridge (#163, Phase-1 spike).

Long-polls the Telegram Bot API ``getUpdates`` and lands each of the boss's
messages into the existing durable answers feed + phone push, and can reply via
``sendMessage``. See :mod:`remote_control.telegram.bridge`.
"""
