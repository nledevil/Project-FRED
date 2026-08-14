"""Event mode — the switch you flip when FRED is in front of the public.

Three separate features wanted this and none of them had it, so each was about
to grow a private version: capping how long an answer runs, capping how fast the
cart may move, and deciding when it is worth spending money looking through the
camera. They are one question — *is he at an event right now?* — and the answer
belongs in one place.

What it means, concretely: a hall full of children is not a workshop. He should
answer shorter, because the mic is deaf while he talks and a child cannot
interrupt him. He should drive slower, because he weighs 350 lb and the people
around him are three feet tall. Nothing here is a safety interlock — the e-stop
and the deadman are — this is about not needing them.

The object is read at call time rather than copied at construction, the same way
``cart_cfg`` is, so the panel can flip it mid-event without restarting anything.
"""
from __future__ import annotations

# Deliberately conservative defaults, because the cost of the two mistakes is
# not symmetric: a cap that is too tight is a slightly terse robot, and one that
# is too loose is the thing you turned event mode on to prevent.
DEFAULT_MAX_WORDS = 25          # spoken answer length; ~10 seconds at Piper's rate
DEFAULT_CART_SPEED = 120        # of SPEED_LIMIT's 300 — see inmoov/cart.py


class EventMode:
    """Whether FRED is at an event, and what that changes.

    Holds the caps as well as the switch so a consumer asks one object rather
    than reaching into settings and re-deriving policy. ``cart_speed`` and
    ``max_words`` return None when it is off, which reads at the call site as
    "no cap" without the caller needing to know why.
    """

    def __init__(self, enabled: bool = False, max_words: int = DEFAULT_MAX_WORDS,
                 cart_speed: int = DEFAULT_CART_SPEED):
        self.enabled = bool(enabled)
        self._max_words = max(1, int(max_words))
        self._cart_speed = max(1, int(cart_speed))

    def set(self, on: bool) -> bool:
        self.enabled = bool(on)
        return self.enabled

    def configure(self, max_words: int | None = None,
                  cart_speed: int | None = None) -> None:
        if max_words is not None:
            self._max_words = max(1, int(max_words))
        if cart_speed is not None:
            self._cart_speed = max(1, int(cart_speed))

    @property
    def max_words(self) -> int | None:
        """Words an answer may run to, or None for no cap."""
        return self._max_words if self.enabled else None

    @property
    def cart_speed(self) -> int | None:
        """Speed ceiling for the drive base, or None for no cap."""
        return self._cart_speed if self.enabled else None

    def status(self) -> dict:
        """What the panel shows. The caps are reported whether or not they are
        in force, so the switch can be read as "this is what it would do"."""
        return {"enabled": self.enabled,
                "max_words": self._max_words,
                "cart_speed": self._cart_speed}
