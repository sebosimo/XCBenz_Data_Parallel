"""Apply the forecast runtime's host headroom limits before starting W work."""

import os
from pathlib import Path
import sys
import time


def cpu_times():
    values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    return sum(values), values[3] + values[4]


def ready(available_mb, cpu_percent, load1, cpus):
    return (available_mb >= 4096 and cpu_percent <= 88
            and load1 <= 1.1 * max(1, cpus))


def main():
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        fields = line.split()
        memory[fields[0].rstrip(':')] = int(fields[1])
    before = cpu_times()
    time.sleep(.15)
    after = cpu_times()
    total, idle = after[0] - before[0], after[1] - before[1]
    cpu = 100 * (1 - idle / total) if total else 0
    available = memory.get('MemAvailable', 0) / 1024
    if not ready(available, cpu, os.getloadavg()[0], os.cpu_count() or 1):
        print('W deferred: host RAM, CPU or load is outside the existing forecast limits.', flush=True)
        raise SystemExit(75)
    os.execv(sys.executable, [sys.executable, '-u', '-m', 'w_forecast.runner', *sys.argv[1:]])


if __name__ == '__main__':
    main()
