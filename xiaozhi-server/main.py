import asyncio
from core.server import XiaozhiServer

if __name__ == "__main__":
    server = XiaozhiServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("Server stopped.")
