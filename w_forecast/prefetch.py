"""One file processing and at most one next file downloading, never two decoders."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event


@contextmanager
def downloads(horizons, fetch, directory, *, prefetch=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {h: directory / f"W_H{h:03d}.grib2" for h in horizons}
    cancelled = Event()
    pool = ThreadPoolExecutor(max_workers=1) if prefetch else None

    def task(horizon):
        return fetch(horizon, paths[horizon], cancelled)

    def iterate():
        future = pool.submit(task, horizons[0]) if pool and horizons else None
        for index, horizon in enumerate(horizons):
            metadata = future.result() if pool else task(horizon)
            if pool and index + 1 < len(horizons):
                future = pool.submit(task, horizons[index + 1])
            try:
                yield horizon, paths[horizon], metadata
            finally:
                paths[horizon].unlink(missing_ok=True)

    iterator = iterate()
    try:
        yield iterator
    finally:
        cancelled.set()
        iterator.close()
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)
        for path in paths.values():
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)
