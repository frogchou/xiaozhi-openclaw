import asyncio
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve

from config import Config
from services.openclaw_bridge import get_openclaw_bridge_service
from utils.logger import logger
from .session import Session

class XiaozhiServer:
    def __init__(self):
        self.sessions = {}
        self._config_lock = asyncio.Lock()
        self._openclaw_bridge_service = get_openclaw_bridge_service()

    def _get_request_headers(self, websocket):
        headers = getattr(websocket, "request_headers", None)
        if headers is not None:
            return headers

        request = getattr(websocket, "request", None)
        if request is not None:
            return getattr(request, "headers", {}) or {}

        return {}

    def _get_request_path(self, websocket):
        request = getattr(websocket, "request", None)
        if request is not None:
            return getattr(request, "path", "/") or "/"
        return "/"

    async def handler(self, websocket):
        request_path = self._get_request_path(websocket)
        parsed_path = urlsplit(request_path)
        if parsed_path.path == Config.OPENCLAW_BRIDGE_PATH:
            token = ""
            try:
                token = (parse_qs(parsed_path.query).get("token") or [""])[0]
            except Exception:
                token = ""

            if Config.OPENCLAW_BRIDGE_TOKEN and token != Config.OPENCLAW_BRIDGE_TOKEN:
                logger.error("Unauthorized OpenClaw bridge connection")
                try:
                    await websocket.close(code=4401, reason="Unauthorized")
                except Exception:
                    pass
                return

            await self._openclaw_bridge_service.handle_connection(websocket)
            return

        headers = self._get_request_headers(websocket)
        device_id = headers.get("Device-Id", "unknown_device")
        client_id = headers.get("Client-Id", "unknown_client")
        auth_header = headers.get("Authorization", "")

        protocol_version_raw = headers.get("Protocol-Version", "1")
        try:
            protocol_version = int(protocol_version_raw)
        except (TypeError, ValueError):
            protocol_version = 1

        if Config.ACCESS_TOKEN:
            expected = f"Bearer {Config.ACCESS_TOKEN}"
            if auth_header != expected:
                logger.error(f"Unauthorized connection from {device_id} ({client_id})")
                try:
                    await websocket.close(code=4401, reason="Unauthorized")
                except Exception:
                    pass
                return

        logger.info(f"New connection: {device_id}/{client_id} (Version: {protocol_version})")
        
        session = Session(websocket, device_id, protocol_version, client_id=client_id)
        session.bind_server_hooks(reload_config=self.update_config)
        self.sessions[session.id] = session

        try:
            await session.run()
        finally:
            try:
                await session.abort()
            except Exception:
                pass
            if session.id in self.sessions:
                del self.sessions[session.id]

    async def update_config(self) -> None:
        async with self._config_lock:
            Config.reload()
            logger.info("Config reloaded")

    def _process_request(self, connection, request):
        try:
            upgrade = request.headers.get("Upgrade", "")
            if request.path in ("/", "/healthz") and str(upgrade).lower() != "websocket":
                return connection.respond(HTTPStatus.OK, "Server is running\n")
        except Exception:
            return connection.respond(HTTPStatus.OK, "Server is running\n")
        return None

    async def start(self):
        logger.info(f"Starting server on {Config.HOST}:{Config.PORT}")
        async with serve(
            self.handler,
            Config.HOST,
            Config.PORT,
            process_request=self._process_request,
            ping_interval=Config.WS_PING_INTERVAL,
            ping_timeout=Config.WS_PING_TIMEOUT,
            max_size=Config.WS_MAX_SIZE,
            max_queue=Config.WS_MAX_QUEUE,
        ) as server:
            await server.serve_forever()
