import asyncio
from BotOrchestrator import BotOrchestrator

if __name__ == "__main__":
    bot = BotOrchestrator()
    asyncio.run(bot.run())
