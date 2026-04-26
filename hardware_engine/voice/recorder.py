"""voice/recorder — VAD config + arecord process gate.

S2/S3/S4/S6 fixes:
    S3/S4 — auto-trigger / MVC playback overlap with mic recording.
            ArecordGate.suspend()/resume() uses SIGSTOP/SIGCONT on arecord
            (no kill, just pause), so the kernel buffer is preserved.
    S6   — long-monologue overshoot. VADConfig.active_speech_cap forces
           a break even when the user keeps talking past hard_cap.

Notes on migration: the actual record_with_vad() body still lives in
voice_daemon.py because it depends on module-level globals (DEVICE_REC,
REC_RATE, ASR_RATE, _VAD_BASELINE_CACHE). VADConfig.apply_to_voice_daemon()
bridges new defaults into those globals at boot time so the new caps take
effect without a deeper rewrite.
"""
import logging
import subprocess


class VADConfig(object):
    """Frozen VAD config. Python 3.7 compatible (no dataclass(slots=))."""
    __slots__ = ("silence_end", "hard_cap", "active_speech_cap", "pre_roll", "_frozen")

    def __init__(self,
                 silence_end=1.0,
                 hard_cap=6.0,
                 active_speech_cap=5.0,
                 pre_roll=0.3):
        object.__setattr__(self, "silence_end", silence_end)
        object.__setattr__(self, "hard_cap", hard_cap)
        object.__setattr__(self, "active_speech_cap", active_speech_cap)
        object.__setattr__(self, "pre_roll", pre_roll)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, k, v):
        if getattr(self, "_frozen", False):
            raise AttributeError("VADConfig is frozen")
        object.__setattr__(self, k, v)

    def apply_to_voice_daemon(self, voice_daemon_module):
        """Override voice_daemon's module-level VAD_TIMEOUT / SILENCE_LIMIT.

        Call this once at boot before record_with_vad() is invoked.
        """
        voice_daemon_module.VAD_TIMEOUT = int(self.hard_cap)
        voice_daemon_module.SILENCE_LIMIT = float(self.silence_end)
        if hasattr(voice_daemon_module, "ACTIVE_SPEECH_CAP"):
            voice_daemon_module.ACTIVE_SPEECH_CAP = float(self.active_speech_cap)


class ArecordGate(object):
    """Pause/resume *all* arecord processes via SIGSTOP/SIGCONT.

    SIGSTOP doesn't kill — the process resumes mid-frame on SIGCONT,
    keeping the kernel-side audio buffer intact. This is what makes the
    BUSY → LISTEN handoff seamless when the system is talking.
    """

    def __init__(self):
        self._suspended = False

    @property
    def suspended(self):
        return self._suspended

    def suspend(self):
        if self._suspended:
            return
        try:
            subprocess.run(["sudo", "killall", "-SIGSTOP", "arecord"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           check=False, timeout=2)
            self._suspended = True
            logging.info(u"[ARECORD_GATE] suspended (SIGSTOP)")
        except Exception as e:
            logging.warning(u"[ARECORD_GATE] suspend failed: %s", e)

    def resume(self):
        if not self._suspended:
            return
        try:
            subprocess.run(["sudo", "killall", "-SIGCONT", "arecord"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           check=False, timeout=2)
            self._suspended = False
            logging.info(u"[ARECORD_GATE] resumed (SIGCONT)")
        except Exception as e:
            logging.warning(u"[ARECORD_GATE] resume failed: %s", e)
