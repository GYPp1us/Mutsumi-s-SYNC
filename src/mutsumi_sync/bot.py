import asyncio
from config import load_config


async def main():
    config = load_config()
    print("Mutsumi's SYNC started")
    # TODO: 启动 WebSocket 监听


if __name__ == "__main__":
    asyncio.run(main())
