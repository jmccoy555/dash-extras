#!/usr/bin/env python3
"""Continuous dashcam recorder for the Anker webcam.

Loop-records in fixed-length segments and deletes the oldest footage once
total size exceeds a cap, so it can just run forever from boot without
filling the disk. Restarts ffmpeg if it ever exits (e.g. camera unplugged).
"""

import glob
import os
import subprocess
import time

# Stable udev by-id path rather than /dev/videoN, which renumbers across
# reboots depending on USB enumeration order. Find the current one with
# `ls /dev/v4l/by-id/` if the camera is ever replaced.
DEVICE = "/dev/v4l/by-id/usb-Anker_PowerConf_C200_Anker_PowerConf_C200_ACNV9P1D24137607-video-index0"
FOOTAGE_DIR = "/home/dash/dashcam-footage"
SEGMENT_SECONDS = 300
MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
RETENTION_CHECK_SECONDS = 30


def enforce_retention_cap():
    files = sorted(glob.glob(os.path.join(FOOTAGE_DIR, "dashcam_*.mp4")), key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    while total > MAX_TOTAL_BYTES and files:
        oldest = files.pop(0)
        try:
            total -= os.path.getsize(oldest)
            os.remove(oldest)
            print(f"Deleted old footage: {oldest}")
        except OSError as e:
            print(f"Failed to delete {oldest}: {e}")


def record_forever():
    os.makedirs(FOOTAGE_DIR, exist_ok=True)
    while True:
        enforce_retention_cap()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720", "-framerate", "30",
            "-i", DEVICE,
            # Camera is mounted upside down - hflip+vflip is a pure pixel
            # reorder equivalent to a 180 rotation, cheaper than a generic
            # rotate filter. Output framerate dropped to 15fps: at
            # 1080p30 libx264 (software - no rkmpp-enabled ffmpeg build is
            # available on this system) cost ~275% CPU running forever in
            # the background, which is too much for an always-on service;
            # 720p15 costs ~100% (one core), a dashcam doesn't need more.
            "-vf", "hflip,vflip",
            "-r", "15",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", "1.5M",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(FOOTAGE_DIR, "dashcam_%Y%m%d_%H%M%S.mp4"),
        ]
        proc = subprocess.Popen(cmd)
        while proc.poll() is None:
            time.sleep(RETENTION_CHECK_SECONDS)
            enforce_retention_cap()
        print("ffmpeg exited, restarting in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    record_forever()
