"""One cross-process FIFO lease for foreground-only work.

The lease is released after the provider submit boundary, before waiting for
the provider's result.  It does not retry or dispatch requests itself.
"""
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import time

from foreground_queue import ForegroundQueue

log = logging.getLogger(__name__)


class ForegroundLease:
    def __init__(self):
        self.queue = ForegroundQueue(Path.home() / '.local/state/creative-unified-workbench/foreground-input')
        self._manager = None
        self.owner = None
        self._submit_clicked_at = None

    @property
    def active(self):
        return self._manager is not None

    async def acquire(self):
        if self.active:
            raise RuntimeError('foreground_lease_already_active')
        manager = self.queue.acquire_async(timeout=600)
        await manager.__aenter__()
        self._manager = manager
        self.owner = asyncio.current_task()
        return self

    def mark_submit_click(self):
        """Start timing just before issuing the click while this lease is held."""
        self._submit_clicked_at = time.monotonic()

    async def release(self):
        manager = self._manager
        if manager is None:
            return
        if self.owner is not asyncio.current_task():
            raise RuntimeError('foreground_lease_wrong_owner')
        clicked_at = self._submit_clicked_at
        self._manager = None
        self.owner = None
        self._submit_clicked_at = None
        await manager.__aexit__(None, None, None)
        if clicked_at is not None:
            elapsed_ms = round((time.monotonic() - clicked_at) * 1000)
            try:
                from task_coordinator import record
                record('submit_to_focus_release', elapsed_ms=elapsed_ms)
            except Exception:
                log.exception('Unable to record Flow desktop lease release timing')

    async def try_acquire(self):
        if self.active:raise RuntimeError('foreground_lease_already_active')
        manager=self.queue.try_acquire_async()
        acquired=await manager.__aenter__()
        if not acquired:
            await manager.__aexit__(None,None,None);return False
        self._manager=manager;self.owner=asyncio.current_task();return True

    @asynccontextmanager
    async def interaction(self, driver, target, page):
        """Reenter for a later UI-only recovery/download action when necessary."""
        if self.active:
            if self.owner is not asyncio.current_task():
                raise RuntimeError('foreground_lease_wrong_owner')
            yield
            return
        await self.acquire()
        try:
            if not await driver.activate_and_confirm(target):
                raise ValueError('desktop_focus_unconfirmed')
            await page.bring_to_front()
            if not await driver._os_focus_confirmed(target):
                raise ValueError('desktop_focus_unconfirmed')
            yield
        finally:
            await self.release()
