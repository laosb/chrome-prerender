import os
import asyncio
import logging
from multiprocessing import cpu_count
from typing import Dict

from websockets.exceptions import InvalidHandshake, ConnectionClosed

from .chromerdp import ChromeRemoteDebugger, Page, TemporaryBrowserFailure

logger = logging.getLogger(__name__)

PRERENDER_TIMEOUT: int = int(os.environ.get('PRERENDER_TIMEOUT', 30))
CONCURRENCY_PER_WORKER: int = int(os.environ.get('CONCURRENCY', cpu_count() * 2))
MAX_ITERATIONS: int = int(os.environ.get('ITERATIONS', 200))


class Prerender:
    def __init__(self, host: str = 'localhost', port: int = 9222, loop=None):
        self.host = host
        self.port = port
        self.loop = loop
        self._rdp = ChromeRemoteDebugger(host, port, loop=loop)
        self._pages = set()
        self._idle_pages = asyncio.Queue(loop=self.loop)

    async def bootstrap(self) -> None:
        for i in range(CONCURRENCY_PER_WORKER):
            page = await self._rdp.new_page()
            await self._idle_pages.put(page)
            self._pages.add(page)

    async def pages(self) -> Dict:
        return await self._rdp.pages()

    async def version(self) -> Dict:
        return await self._rdp.version()

    async def shutdown(self) -> None:
        for page in self._pages:
            await page.close()
        self._rdp.shutdown()

    async def render(self, url: str) -> str:
        if not self._pages:
            raise RuntimeError('No browser available')

        try:
            page = await asyncio.wait_for(self._idle_pages.get(), timeout=10)
        except asyncio.TimeoutError:
            raise TemporaryBrowserFailure('No Chrome page available in 10s')

        reopen = False
        try:
            await page.attach()
            try:
                await asyncio.wait_for(page.listen(), timeout=1)
            except asyncio.TimeoutError:
                logger.error('Attach to Chrome page %s timed out in 1s, page is likely closed', page.id)
                reopen = True
                raise TemporaryBrowserFailure('Attach to Chrome page timed out')
            await page.navigate(url)
            html = await asyncio.wait_for(page.wait(), timeout=PRERENDER_TIMEOUT)
            return html
        except InvalidHandshake:
            logger.error('Chrome invalid handshake for page %s', page.id)
            reopen = True
            raise TemporaryBrowserFailure('Invalid handshake')
        except ConnectionClosed:
            logger.error('Chrome remote connection closed for page %s', page.id)
            reopen = True
            raise TemporaryBrowserFailure('Chrome remote debugging connection closed')
        except RuntimeError as e:
            # https://github.com/MagicStack/uvloop/issues/68
            if 'unable to perform operation' in str(e):
                reopen = True
                raise TemporaryBrowserFailure(str(e))
            else:
                raise
        finally:
            await asyncio.shield(self._manage_page(page, reopen))

    async def _manage_page(self, page: Page, reopen: bool = False) -> None:
        self._idle_pages.task_done()
        if page.websocket:
            if not reopen:
                await page.navigate('about:blank')  # Saves memory
            await page.detach()

        if not reopen and page.iteration < MAX_ITERATIONS:
            await self._idle_pages.put(page)
            return

        await page.close()
        self._pages.remove(page)
        page = await self._rdp.new_page()
        # wait until Chrome is ready
        await asyncio.sleep(0.1)
        await self._idle_pages.put(page)
        self._pages.add(page)
        logger.info('Page %s added to idle pages queue', page.id)
