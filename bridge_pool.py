"""Independent admission, thread budgets and visible slots for each backend."""
from contextlib import asynccontextmanager
import anyio

from bridge_config import PROVIDERS


class BackendPool:
    def __init__(self, key, backend, capacity):
        self.key, self.backend, self.capacity = key, backend, capacity
        self.gate = anyio.Semaphore(capacity)
        # Do not silently inherit AnyIO's default shared 40-worker ceiling.
        self.workers = anyio.CapacityLimiter(capacity)
        self.occupied = set()
        self.waiting = 0

    @asynccontextmanager
    async def admit(self):
        self.waiting += 1
        admitted = False
        try:
            async with self.gate:
                self.waiting -= 1
                admitted = True
                slot = 0
                while slot in self.occupied:
                    slot += 1
                self.occupied.add(slot)
                try:
                    yield
                finally:
                    self.occupied.remove(slot)
        finally:
            if not admitted:
                self.waiting -= 1

    def snapshot(self):
        backend = self.backend
        return {
            'provider': self.key, 'name': PROVIDERS[self.key]['name'],
            'model': PROVIDERS[self.key]['model'], 'backend': getattr(backend, 'backend_id', 'wechat-4.1.13.65'),
            'max_concurrency': self.capacity, 'active_requests': len(self.occupied),
            'occupied_slots': sorted(self.occupied),
            'queued_requests': self.waiting,
            'status': 'ok' if backend.healthy else 'degraded',
            'last_error': backend.last_error, 'last_success': backend.last_success,
            'last_error_detail': getattr(backend, 'last_error_detail', None),
            'warnings': list(getattr(backend, 'warnings', [])),
            'agent_connected': backend.agent_connected, 'last_pid': backend.last_pid,
            'completion': getattr(backend, 'last_completion', None),
        }
