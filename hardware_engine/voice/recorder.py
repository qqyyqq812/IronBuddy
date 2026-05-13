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
import os
import subprocess


class VADConfig(object):
    """Frozen VAD config. Python 3.7 compatible (no dataclass(slots=))."""
    __slots__ = ("silence_end", "hard_cap", "active_speech_cap", "pre_roll", "_frozen")

    def __init__(self,
                 silence_end=None,
                 hard_cap=6.0,
                 active_speech_cap=5.0,
                 pre_roll=0.3):
        if silence_end is None:
            silence_end = float(os.environ.get("VOICE_SILENCE_LIMIT", "2.0"))
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

    def _signal(self, sig, process_names):
        """Send a non-interactive sudo killall signal and report real success."""
        cmd = ["sudo", "-n", "killall", sig] + list(process_names)
        try:
            ret = subprocess.run(cmd,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 check=False, timeout=2)
            rc = getattr(ret, "returncode", 0)
            if rc == 0:
                return True
            logging.warning(u"[ARECORD_GATE] %s failed rc=%s targets=%s",
                            sig, rc, ",".join(process_names))
        except Exception as e:
            logging.warning(u"[ARECORD_GATE] %s failed: %s", sig, e)
        return False

    @property
    def suspended(self):
        return self._suspended

    def suspend(self):
        if self._suspended:
            return
        if self._signal("-SIGSTOP", ["arecord"]):
            self._suspended = True
            logging.info(u"[ARECORD_GATE] suspended (SIGSTOP)")

    def resume(self):
        if not self._suspended:
            return
        # Resume both arecord and its sudo wrapper. On the board, a stopped
        # root-owned "sudo arecord" parent can otherwise survive as a frozen
        # wrapper even after the arecord child exits.
        if self._signal("-SIGCONT", ["arecord", "sudo"]):
            self._suspended = False
            logging.info(u"[ARECORD_GATE] resumed (SIGCONT)")
