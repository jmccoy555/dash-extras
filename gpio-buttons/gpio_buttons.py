#!/usr/bin/env python3

import gpiod
import time
from pynput.keyboard import Controller

# Set the GPIO pins for buttons
gpio_chip = '/dev/gpiochip3'  # Default GPIO chip
button_pins = [13, 15, 19, 9, 14]  # GPIO pins for buttons

# Open the GPIO chip
chip = gpiod.Chip(gpio_chip)

# Get the lines corresponding to the buttons
lines = [chip.get_line(pin) for pin in button_pins]

# Set up the lines for input with pull-up resistors (assuming buttons are connected to ground)
for line in lines:
    line.request(consumer="gpio_keyboard", type=gpiod.LINE_REQ_DIR_IN, default_val=1)

# Initialize the keyboard controller
keyboard = Controller()

# Define what key each button corresponds to
button_to_key = {
    13: '1',  # GPIO 17 -> '1'
#    15: '2',  # GPIO 18 -> '2'
#    19: '3',  # GPIO 19 -> '3'
#    9: '4',  # GPIO 19 -> '4'
#    14: '5'
}

# Function to simulate key press when button is pressed
def simulate_key_press(pin):
    if pin in button_to_key:
        key = button_to_key[pin]
        print(f"Button pressed! Simulating key press: {key}")
        keyboard.press(key)
        keyboard.release(key)

# Loop to monitor the button presses
try:
    while True:
        for idx, line in enumerate(lines):
            if line.get_value() == 0:  # Detect if button is pressed (assuming active low)
                simulate_key_press(button_pins[idx])
                time.sleep(0.1)  # Debounce time, adjust as needed
        time.sleep(0.01)

except KeyboardInterrupt:
    print("Program terminated.")
