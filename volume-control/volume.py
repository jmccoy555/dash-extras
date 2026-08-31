#!/usr/bin/env python3
"""Rotary encoder volume control for the dash's front-panel knob."""

import gpiod
import subprocess
import time

GPIO_CHIP = "/dev/gpiochip3"
PIN_A = 18
PIN_B = 17
STEP = 2
VOLUME_MIN = 0
VOLUME_MAX = 99

# Quadrature decoder state machine (Ben Buxton's "rotary" algorithm,
# public domain). A turn is only registered after a complete, valid
# sequence of A/B transitions, so a single noisy edge or a sample the
# 1ms poll below happens to miss mid-turn can't look like a spurious
# direction change - which is what caused the old "compare A to B on
# every A edge" approach to bounce when the knob was turned quickly.
R_START = 0x0
R_CW_FINAL = 0x1
R_CW_BEGIN = 0x2
R_CW_NEXT = 0x3
R_CCW_BEGIN = 0x4
R_CCW_FINAL = 0x5
R_CCW_NEXT = 0x6
DIR_CW = 0x10
DIR_CCW = 0x20

TRANSITION_TABLE = [
    [R_START, R_CW_BEGIN, R_CCW_BEGIN, R_START],
    [R_CW_NEXT, R_START, R_CW_FINAL, R_START | DIR_CW],
    [R_CW_NEXT, R_CW_BEGIN, R_START, R_START],
    [R_CW_NEXT, R_CW_BEGIN, R_CW_FINAL, R_START],
    [R_CCW_NEXT, R_START, R_CCW_BEGIN, R_START],
    [R_CCW_NEXT, R_CCW_FINAL, R_START, R_START | DIR_CCW],
    [R_CCW_NEXT, R_CCW_FINAL, R_CCW_BEGIN, R_START],
]


def get_current_volume():
    try:
        result = subprocess.run(["amixer", "get", "Master"], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            if "Front Left:" in line or "Mono:" in line:
                for word in line.split():
                    if "%" in word:
                        return int(word.strip("[]%"))
    except subprocess.CalledProcessError as e:
        print(f"Error getting volume: {e}")
    except ValueError:
        print("Failed to convert volume value to integer.")
    return 20


def change_volume(level):
    try:
        subprocess.run(["amixer", "set", "Master", f"{level}%"], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to set volume: {e}")


chip = gpiod.Chip(GPIO_CHIP)
line_a = chip.get_line(PIN_A)
line_b = chip.get_line(PIN_B)
line_a.request(consumer="rotary_encoder", type=gpiod.LINE_REQ_DIR_IN)
line_b.request(consumer="rotary_encoder", type=gpiod.LINE_REQ_DIR_IN)

volume = get_current_volume()
state = R_START

try:
    while True:
        pin_state = (line_a.get_value() << 1) | line_b.get_value()
        state = TRANSITION_TABLE[state & 0xF][pin_state]

        if state & DIR_CW:
            volume = min(volume + STEP, VOLUME_MAX)
            change_volume(volume)
            print(f"Encoder position: {volume}%")
        elif state & DIR_CCW:
            volume = max(volume - STEP, VOLUME_MIN)
            change_volume(volume)
            print(f"Encoder position: {volume}%")

        # Tighter than the old 10ms poll - a fast manual turn can cross a
        # quadrature state in only a few ms, and the old interval was slow
        # enough to skip states, which is what fed bad transitions into the
        # (previously naive) decoder. The state machine above no longer
        # needs this for correctness, but a 0% sleep would busy-loop a core.
        time.sleep(0.001)

except KeyboardInterrupt:
    print("Program terminated.")
