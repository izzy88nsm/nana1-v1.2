import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from aiohttp import web

logger = logging.getLogger(__name__)

class HealthCheckServer:
    """A configurable health check server for async and sync endpoints with timeout support."""

    def __init__(
        self,
        host: str = '0.0.0.0',
        port: int = 8080,
        endpoints: Optional[Dict[str, Callable[[], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]]]] = None,
        timeout: float = 30.0,
        route_prefix: str = '/health',
middlewares: Optional[List[Callable[[web.Request, Callable], Awaitable[web.StreamResponse]]]] = None,
    ):
        self.host = host
        self.port = port
        self.endpoints = dict(endpoints) if endpoints else {}
        self.timeout = timeout
        self.route_prefix = route_prefix.rstrip('/')
        self.middlewares = middlewares or []
        self._runner: Optional[web.AppRunner] = None

        if 'default' not in self.endpoints:
            self.endpoints['default'] = lambda: {'status': 'ok'}

        for endpoint_name, check_func in self.endpoints.items():
            if not callable(check_func):
                raise TypeError(f"Endpoint '{endpoint_name}' must be a callable.")

    async def _handle(self, request: web.Request) -> web.Response:
        path = request.match_info.get('endpoint', 'default')
        logger.debug("Starting health check for endpoint: %s", path)

        check = self.endpoints.get(path)
        if not check:
            logger.warning("Unknown health check endpoint requested: %s", path)
            return web.json_response(
                {"status": "error", "message": f"Unknown endpoint '{path}'"},
                status=404
            )

        try:
            if inspect.iscoroutinefunction(check):
                coro = check()
            else:
                loop = asyncio.get_event_loop()
                coro = loop.run_in_executor(None, check)

            result = await asyncio.wait_for(coro, timeout=self.timeout)
            return web.json_response({"status": "ok", "details": result})

        except asyncio.TimeoutError:
            logger.error("Health check '%s' timed out after %.1fs", path, self.timeout)
            return web.json_response(
                {"status": "error", "message": f"Check timed out after {self.timeout}s"},
                status=503
            )
        except Exception as e:
            logger.exception("Health check '%s' failed with error: %s", path, str(e))
            return web.json_response(
                {"status": "error", "message": str(e)},
                status=500
            )

    async def start(self) -> None:
        app = web.Application(middlewares=self.middlewares)
        base_route = self.route_prefix
        endpoint_route = f"{self.route_prefix}/{{endpoint}}"

        app.router.add_get(base_route, self._handle)
        app.router.add_get(endpoint_route, self._handle)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

        logger.info("HealthCheckServer running on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("HealthCheckServer stopped")

