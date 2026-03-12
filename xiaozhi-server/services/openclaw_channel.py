import asyncio
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import Config
from utils.logger import logger


class _OpenClawCallbackHandler(BaseHTTPRequestHandler):
    server: "_OpenClawCallbackHttpServer"

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("OpenClaw callback server: " + format % args)

    def _write_json(self, status_code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path != self.server.callback_path:
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return

        expected_token = Config.OPENCLAW_CALLBACK_TOKEN
        if expected_token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {expected_token}":
                self._write_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized"})
                return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length"})
            return

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON"})
            return

        request_id = str(payload.get("requestId", "")).strip()
        if not request_id:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing requestId"})
            return

        self.server.service.handle_callback(payload)
        self._write_json(HTTPStatus.OK, {"ok": True})


class _OpenClawCallbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        callback_path: str,
        service: "OpenClawChannelService",
    ) -> None:
        super().__init__(server_address, _OpenClawCallbackHandler)
        self.callback_path = callback_path
        self.service = service


class OpenClawChannelService:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[_OpenClawCallbackHttpServer] = None
        self._thread: Optional[threading.Thread] = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._early_callbacks: dict[str, dict[str, Any]] = {}
        self._started = False

    def is_configured(self) -> bool:
        return bool(Config.OPENCLAW_CHANNEL_URL and Config.OPENCLAW_CHANNEL_TOKEN)

    def callback_url(self) -> str:
        if Config.OPENCLAW_CALLBACK_URL:
            return Config.OPENCLAW_CALLBACK_URL
        return (
            f"http://{Config.OPENCLAW_CALLBACK_PUBLIC_HOST}:"
            f"{Config.OPENCLAW_CALLBACK_PORT}{Config.OPENCLAW_CALLBACK_PATH}"
        )

    async def start(self) -> None:
        if self._started:
            return

        self._loop = asyncio.get_running_loop()
        self._server = _OpenClawCallbackHttpServer(
            (Config.OPENCLAW_CALLBACK_BIND_HOST, Config.OPENCLAW_CALLBACK_PORT),
            Config.OPENCLAW_CALLBACK_PATH,
            self,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="openclaw-callback-server",
            daemon=True,
        )
        self._thread.start()
        self._started = True
        logger.info(
            "OpenClaw callback server listening on "
            f"{Config.OPENCLAW_CALLBACK_BIND_HOST}:{Config.OPENCLAW_CALLBACK_PORT}"
            f"{Config.OPENCLAW_CALLBACK_PATH}"
        )

    async def stop(self) -> None:
        if not self._started:
            return

        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        self._started = False

        if server is not None:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)

        if thread is not None:
            await asyncio.to_thread(thread.join, 1.0)

        pending = list(self._pending.values())
        self._pending.clear()
        self._early_callbacks.clear()
        for future in pending:
            if not future.done():
                future.cancel()

    async def reload(self) -> None:
        await self.stop()
        await self.start()

    def handle_callback(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            logger.warning("OpenClaw callback received before event loop was ready")
            return

        request_id = str(payload.get("requestId", "")).strip()
        if not request_id:
            return

        def _deliver() -> None:
            future = self._pending.pop(request_id, None)
            if future is None:
                self._early_callbacks[request_id] = payload
                return
            if not future.done():
                future.set_result(payload)

        loop.call_soon_threadsafe(_deliver)

    async def request_reply(
        self,
        user_text: str,
        from_id: str,
        conversation_id: str,
        sender_name: Optional[str] = None,
    ) -> str:
        if not self.is_configured():
            logger.warning("OpenClaw channel config missing, skipping LLM request")
            return "Server: OpenClaw channel config missing"

        if not self._started:
            await self.start()

        body = {
            "text": user_text,
            "from": from_id,
            "senderName": sender_name or from_id,
            "conversationId": conversation_id,
            "callbackUrl": self.callback_url(),
            "callbackToken": Config.OPENCLAW_CALLBACK_TOKEN,
        }

        response = await asyncio.to_thread(self._post_json, Config.OPENCLAW_CHANNEL_URL, body)
        request_id = str(response.get("requestId", "")).strip()
        if not request_id:
            raise RuntimeError(f"OpenClaw channel response missing requestId: {response}")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        early = self._early_callbacks.pop(request_id, None)
        if early is not None:
            future.set_result(early)
        else:
            self._pending[request_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=Config.OPENCLAW_CHANNEL_TIMEOUT_SECONDS)
        except Exception:
            pending = self._pending.get(request_id)
            if pending is future:
                self._pending.pop(request_id, None)
            raise

        if not result.get("ok", False):
            raise RuntimeError(str(result.get("error", "OpenClaw callback error")))

        return str(result.get("reply", "")).strip()

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.OPENCLAW_CHANNEL_TOKEN}",
        }
        request = Request(url=url, data=payload, headers=headers, method="POST")

        try:
            with urlopen(request, timeout=Config.OPENCLAW_CHANNEL_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"OpenClaw channel HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"OpenClaw channel connection error: {exc}") from exc


_service: Optional[OpenClawChannelService] = None


def get_openclaw_channel_service() -> OpenClawChannelService:
    global _service
    if _service is None:
        _service = OpenClawChannelService()
    return _service
