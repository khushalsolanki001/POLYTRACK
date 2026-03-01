import asyncio
import api
async def test():
    trades = await api.fetch_trades("0x0000000000000000000000000000000000000000", limit=1)
    if trades:
        print(trades)
    else:
        print("no trades found for that generic wallet")
asyncio.run(test())
