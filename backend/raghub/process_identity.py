"""Process start identity, independent of wall-clock corrections on macOS."""

import psutil


def process_start_time(process: psutil.Process) -> float:
    # The pinned psutil 7.2.2 Darwin backend adjusts public create_time()
    # relative to the importing process's boot-time snapshot. Two services
    # can therefore disagree after a clock update. Use the same unadjusted
    # kernel timestamp that psutil uses for its own PID-reuse identity.
    if psutil.OSX:
        return process._proc.create_time(monotonic=True)
    return process.create_time()
