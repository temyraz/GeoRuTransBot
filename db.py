"""
Слой работы с базой данных (SQLite через SQLAlchemy, асинхронно через aiosqlite).

Таблица vocabulary — личный словарь пользователя: сохранённые пары
(исходный текст, перевод), которые можно посмотреть и удалить через /dictionary.
"""

import os
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import BigInteger, DateTime, Integer, Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Путь к файлу SQLite. На PaaS-хостингах вроде Railway диск контейнера обычно
# эфемерный — если нужно, чтобы словарь переживал редеплой, подключите
# постоянный volume и укажите его путь через DATABASE_PATH (см. README).
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

PAGE_SIZE = 5


class Base(DeclarativeBase):
    pass


class VocabularyEntry(Base):
    __tablename__ = "vocabulary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )


_engine = create_async_engine(DATABASE_URL)
_async_session = async_sessionmaker(_engine, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. Вызывается один раз при старте бота."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def add_vocabulary_entry(user_id: int, original_text: str, translated_text: str) -> int:
    async with _async_session() as session:  # type: AsyncSession
        entry = VocabularyEntry(
            user_id=user_id, original_text=original_text, translated_text=translated_text
        )
        session.add(entry)
        await session.commit()
        return entry.id


async def count_vocabulary_entries(user_id: int) -> int:
    async with _async_session() as session:
        result = await session.execute(
            select(func.count()).select_from(VocabularyEntry).where(VocabularyEntry.user_id == user_id)
        )
        return result.scalar_one()


async def get_vocabulary_page(user_id: int, offset: int, limit: int = PAGE_SIZE) -> Sequence[VocabularyEntry]:
    async with _async_session() as session:
        result = await session.execute(
            select(VocabularyEntry)
            .where(VocabularyEntry.user_id == user_id)
            .order_by(VocabularyEntry.created_at.desc(), VocabularyEntry.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()


async def delete_vocabulary_entry(entry_id: int, user_id: int) -> bool:
    """
    Удаляет запись только если она принадлежит этому user_id — чтобы один
    пользователь не мог удалить запись другого, зная/подобрав её id.
    """
    async with _async_session() as session:
        result = await session.execute(
            delete(VocabularyEntry).where(
                VocabularyEntry.id == entry_id, VocabularyEntry.user_id == user_id
            )
        )
        await session.commit()
        return result.rowcount > 0
