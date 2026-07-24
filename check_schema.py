import asyncio
import sys
from app.schema import get_schema_description

async def main():
    print(await get_schema_description())

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

asyncio.run(main())