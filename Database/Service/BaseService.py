from sqlalchemy.ext.asyncio import AsyncSession


class BaseService:

    @staticmethod
    async def _commit_refresh(session: AsyncSession, *instances):
        await session.commit()
        for instance in instances:
            await session.refresh(instance)