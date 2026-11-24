"""
led_sync.py — LEDs of an answer shown with its sentence and its gesture (no ROS 2).

body_gateway sends the next answer while NAO still plays the current one
(twitch-input prepares it meanwhile).  Its LED order carries the "sync" id of
its sentence and learned gesture: the NAO driver holds it until the "go" of
that sentence on /speech_gesture_sync (sent by the static drone when the
gesture starts, or at once when the gesture is refused), then turns it on
`in_s` seconds later, when the sentence starts.  cognitive_renderer puts its
own profile back when the voice stops speaking, so the color lasts as long as
the sentence and the gesture.

An order whose "go" never comes (gesture service down) is shown anyway after
max_wait_s.
"""


class LedSync:
    """LED orders held until their sentence starts."""

    def __init__(self, max_wait_s: float = 20.0):
        self.max_wait_s = max(0.0, float(max_wait_s))
        self._held: dict[str, tuple[dict, float]] = {}   # sync -> (order, received at)

    def hold(self, sync: str, order: dict, now: float) -> None:
        """Keep the order until the go of `sync` (the latest one per sync)."""
        self._held[str(sync)] = (order, now)

    def go(self, payload: dict, now: float) -> tuple[dict, float] | None:
        """A /speech_gesture_sync message: (order, delay in s before showing it), or None."""
        if not isinstance(payload, dict) or payload.get("kind") != "go":
            return None
        held = self._held.pop(str(payload.get("sync") or ""), None)
        if held is None:
            return None
        try:
            delay = float(payload.get("in_s") or 0.0)
        except (TypeError, ValueError):
            delay = 0.0
        return held[0], max(0.0, min(delay, self.max_wait_s))

    def expired(self, now: float) -> list[dict]:
        """Orders whose go has not come after max_wait_s: shown now."""
        late = [s for s, (_, t) in self._held.items() if now - t >= self.max_wait_s]
        return [self._held.pop(s)[0] for s in late]

    def __len__(self) -> int:
        return len(self._held)
