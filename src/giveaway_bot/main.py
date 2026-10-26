import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from giveaway_bot.config import Settings
from giveaway_bot.db.session import make_engine, make_sessionmaker
from giveaway_bot.handlers import admin, user
from giveaway_bot.ports.beacon import DrandBeacon
from giveaway_bot.ports.membership import TelegramMembership
from giveaway_bot.ports.publisher import TelegramPublisher
from giveaway_bot.services.claims import Claims
from giveaway_bot.services.draw_service import DrawService
from giveaway_bot.services.participation import Participation
from giveaway_bot.services.runner import Runner
from giveaway_bot.services.stats import Stats

log = logging.getLogger("giveaway_bot")


async def run() -> None:
    settings = Settings()
    if not settings.admin_ids:
        sys.exit("ADMIN_IDS is empty: nobody could run draws")

    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await _serve(bot, settings)
    finally:
        await bot.session.close()


async def _serve(bot: Bot, settings: Settings) -> None:
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        sys.exit("BOT_TOKEN was rejected by Telegram")
    membership = TelegramMembership(bot, settings.channel_id)
    if not await membership.bot_is_admin():
        sys.exit(
            f"@{me.username} is not an administrator of {settings.channel_id}; "
            "subscription checks would be unreliable. Add the bot as a channel admin."
        )

    beacon = DrandBeacon(
        settings.drand_relays,
        settings.drand_chain_hash,
        genesis=settings.drand_genesis,
        period=settings.drand_period,
        min_agreeing=settings.drand_min_agreeing,
    )
    await beacon.load_info()  # refuses to start if relays disagree with the pinned chain

    engine = make_engine(settings.db_url)
    sm = make_sessionmaker(engine)
    admin_ids = set(settings.admin_ids)
    publisher = TelegramPublisher(bot, settings.channel_id)
    participation = Participation(sm, membership, bot_admin_ids=admin_ids)
    draws = DrawService(
        sm,
        membership,
        beacon,
        publisher,
        participation,
        admin_ids=admin_ids,
        bot_username=me.username,
        claim_window_hours=settings.claim_window_hours,
        lead_seconds=settings.beacon_lead_seconds,
    )
    claims = Claims(
        sm, publisher, draws, admin_ids=admin_ids, window_hours=settings.claim_window_hours
    )
    stats = Stats(
        sm,
        membership,
        participation,
        admin_ids=admin_ids,
        retention_days=settings.retention_days,
        checks_per_second=settings.retention_checks_per_second,
    )
    runner = Runner(draws, claims, stats)

    dp = Dispatcher(
        settings=settings,
        admin_ids=admin_ids,
        participation=participation,
        draws=draws,
        claims=claims,
        stats=stats,
        runner=runner,
    )
    dp.include_routers(admin.router, user.router)
    dp.startup.register(runner.start)
    dp.shutdown.register(runner.stop)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Танлов / Tanlov / Розыгрыш"),
            BotCommand(command="status", description="Иштирок / Ishtirok / Статус"),
            BotCommand(command="verify", description="Текшириш / Tekshirish / Проверка"),
            BotCommand(command="language", description="Тил / Til / Язык"),
        ]
    )
    log.info("@%s started for %s, admins=%s", me.username, settings.channel_id, sorted(admin_ids))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await beacon.aclose()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
