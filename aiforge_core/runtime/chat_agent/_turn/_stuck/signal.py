"""The detectors' vocabulary: see :mod:`aiforge_core.runtime.stuck_signal`
(neutral, so the pipeline's gates use the same kinds)."""
from aiforge_core.runtime.stuck_signal import (  # noqa: F401
    IDENTICAL_RESULT,
    IDLE_REPLY,
    MONOLOGUE,
    NARRATION,
    NO_PROGRESS,
    PING_PONG,
    SAME_ACTION,
    SAME_FAILURE,
    SAME_OUTPUT,
    Signal,
    short_key,
)
