"""VoiceStateMachine — 3-state explicit machine, replaces voice_daemon's implicit while-True.

Python 3.7 compatible (board constraint): no `X | None`, no `dataclass(slots=)`,
no match-case, no walrus.

States:
    LISTEN  — listening for wake word (default; mic open)
    DIALOG  — wake hit, capturing user input + routing reply
    BUSY    — system is talking / MVC running / auto-trigger active (mic SIGSTOP'd)
"""
import time
import logging
import threading
from enum import Enum
from collections import namedtuple


class VoiceState(Enum):
    LISTEN = "listen"
    DIALOG = "dialog"
    BUSY = "busy"


Transition = namedtuple("Transition", ["from_state", "to_state", "reason", "ts"])


class VoiceStateMachine(object):
    """Explicit 3-state machine. All transitions go through transition()."""

    def __init__(self):
        self._state = VoiceState.LISTEN
        self._enter_ts = time.time()
        self._lock = threading.Lock()
        self.history = []

    @property
    def state(self):
        return self._state

    @property
    def time_in_state(self):
        return time.time() - self._enter_ts

    def transition(self, to_state, reason=""):
        with self._lock:
            from_state = self._state
            now = time.time()
            time_in_prev = now - self._enter_ts
            self._state = to_state
            self._enter_ts = now
            self.history.append(Transition(from_state, to_state, reason, now))
            logging.info(
                u"[STATE] %s -> %s (reason=%s, in_prev=%.1fs)",
                from_state.value, to_state.value, reason, time_in_prev,
            )

    def is_(self, *states):
        return self._state in states
