"""Derive an artificial character-session ID from MumbleLink signals.

MumbleLink doesn't tell us "the player just logged in" directly, but a few
signals together let us synthesize session boundaries reliably:

  * `uiTick == 0`         — game is at character select / loading screen.
                            The *next* in-world sample starts a fresh session.
  * `process_id` change   — GW2 was closed and relaunched (new process).
  * `identity.name` change— character was swapped (logged out and back in
                            on a different character).
  * idle gap > N seconds  — we lost connectivity to MumbleLink for long
                            enough that a logout almost certainly happened
                            in between (default 60s).

A session ID is a short hex string generated when one of these triggers
fires; otherwise it persists across map changes, mounts, combat, etc.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

LOGGER = logging.getLogger("gw2_zerobus.session")

DEFAULT_IDLE_GAP_SECONDS = 60.0


@dataclass
class SessionInfo:
    session_id: str
    session_start_iso: str
    reason: str  # why this session was opened (logged for diagnostics)


class SessionTracker:
    """Stateful detector for character-session boundaries.

    Call `update(sample)` once per polled MumbleLink sample. Returns
    `SessionInfo` when the sample is in-world (uiTick > 0), or None when
    the player is at char-select / loading (uiTick == 0). When a new
    session boundary is crossed, a fresh ID is allocated and logged.
    """

    def __init__(self, idle_gap_seconds: float = DEFAULT_IDLE_GAP_SECONDS) -> None:
        self._idle_gap = float(idle_gap_seconds)
        self._session_id: Optional[str] = None
        self._session_start: Optional[datetime] = None
        self._last_pid: Optional[int] = None
        self._last_name: Optional[str] = None
        self._last_seen_monotonic: Optional[float] = None
        # First in-world sample after construction or after a uiTick=0 burst
        # always opens a session.
        self._pending_login = True

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def update(self, sample) -> Optional[SessionInfo]:
        """Process one MumbleLink sample and return current session info.

        Returns None when the player is not in-world (uiTick == 0). The
        next in-world sample after that will begin a brand-new session.
        """
        ctx = sample.context
        identity = sample.identity or {}
        name = identity.get("name") or ""
        pid = int(ctx.process_id or 0)
        now = time.monotonic()

        if sample.ui_tick == 0:
            # At character select / loading — close the current session,
            # pre-arm a new one for the next in-world sample.
            if not self._pending_login:
                LOGGER.debug("session %s closed: uiTick=0 (logout/loading)",
                             self._session_id)
            self._pending_login = True
            self._last_seen_monotonic = now
            return None

        reason: Optional[str] = None
        if self._session_id is None:
            reason = "first_sample"
        elif self._pending_login:
            reason = "logged_in"
        elif self._last_pid not in (None, 0) and pid not in (0, self._last_pid):
            reason = "process_changed"
        elif self._last_name is not None and name and name != self._last_name:
            reason = "character_changed"
        elif (
            self._last_seen_monotonic is not None
            and now - self._last_seen_monotonic > self._idle_gap
        ):
            reason = "idle_gap"

        if reason is not None:
            self._session_id = uuid.uuid4().hex[:12]
            self._session_start = datetime.now(timezone.utc)
            LOGGER.info(
                "new character session: id=%s reason=%s name=%r pid=%d",
                self._session_id, reason, name, pid,
            )

        self._last_pid = pid
        self._last_name = name
        self._last_seen_monotonic = now
        self._pending_login = False

        assert self._session_id is not None and self._session_start is not None
        return SessionInfo(
            session_id=self._session_id,
            session_start_iso=self._session_start.isoformat(),
            reason=reason or "unchanged",
        )
