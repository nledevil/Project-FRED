#!/usr/bin/env python3
"""A short 'come alive' demo: look around, blink-nod, open the jaw.

Runs safely in mock mode with no hardware (prints what it *would* do).
    ./venv/bin/python demo.py

With hardware wired and I2C live, the same command drives the servos.
"""
import time

from inmoov.servo_controller import ServoController


def main() -> None:
    with ServoController() as ctrl:      # relaxes all servos on exit
        ctrl.rest()
        time.sleep(0.5)

        # glance around
        for eye_x, eye_y in [(75, 90), (110, 90), (90, 75), (90, 110), (90, 90)]:
            ctrl.move_smooth("eye_x", eye_x, duration=0.3)
            ctrl.move_smooth("eye_y", eye_y, duration=0.3)
            time.sleep(0.2)

        # turn the head, eyes leading
        ctrl.move_smooth("eye_x", 110, duration=0.2)
        ctrl.move_smooth("neck", 120, duration=0.8)
        time.sleep(0.3)
        ctrl.move_smooth("eye_x", 90, duration=0.2)
        ctrl.move_smooth("neck", 90, duration=0.8)

        # "talk": a few jaw flaps
        for _ in range(4):
            ctrl.move_smooth("jaw", 60, duration=0.12)
            ctrl.move_smooth("jaw", 20, duration=0.12)

        ctrl.rest()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
