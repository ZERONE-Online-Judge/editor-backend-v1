"""Bounded ordered work with cooperative cancellation and a shared run budget."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from threading import BoundedSemaphore, Event, Lock, local

_context = local()
_budgets = {}
_budget_lock = Lock()


class ParallelStopped(RuntimeError):
    pass


def check_stopped():
    if any(token.is_set() for token in getattr(_context, "tokens", ())):
        raise ParallelStopped("병렬 작업이 종료되어 남은 실행을 취소했습니다.")


@contextmanager
def execution_budget(limit, check):
    # Shared across all job threads in one worker process, including compiles.
    with _budget_lock:
        semaphore = _budgets.setdefault(limit, BoundedSemaphore(limit))
    while not semaphore.acquire(timeout=.1):
        check_stopped()
        check()
    try:
        check_stopped()
        check()
        yield
    finally:
        semaphore.release()


@contextmanager
def ordered_work(function, items, workers, check=lambda: None):
    """Keep at most workers items outstanding; drain before sandbox cleanup.

    Submit the next item only when the consumer advances, so an early stop
    never queues the whole test suite. Results and errors retain input order.
    """
    if workers <= 1:
        def serial():
            for item in items:
                check()
                yield function(item)
        yield serial()
        return
    stop = Event()
    inherited = getattr(_context, "tokens", ())
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="editor-test")
    pending = deque()
    source = iter(items)
    def execute(item):
        previous = getattr(_context, "tokens", ())
        _context.tokens = (*inherited, stop)
        try:
            check_stopped()
            return function(item)
        finally:
            _context.tokens = previous
    def fill():
        while len(pending) < workers:
            try:
                item = next(source)
            except StopIteration:
                break
            pending.append(pool.submit(execute, item))
    def results():
        fill()
        while pending:
            future = pending[0]
            while True:
                check()
                try:
                    value = future.result(timeout=.1)
                    break
                except TimeoutError:
                    # The callable itself may raise TimeoutError.
                    if future.done():
                        value = future.result()
                        break
            pending.popleft()
            yield value
            fill()
    iterator = results()
    try:
        yield iterator
    finally:
        stop.set()
        iterator.close()
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
