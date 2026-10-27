"""The giveaway's way in from the channel: the «🎁 Иштирок этиш» button on every post the bot
publishes, and the announcement post admins publish from /admin. The button deep-links into
the bot; one Start tap enters a subscribed user."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from giveaway_bot.db.models import AuditLog, Setting, utcnow
from giveaway_bot.i18n import CHANNEL_LANG, t
from giveaway_bot.ports.publisher import Button, Keyboard, Publisher

# /start parameter of the button, stored as the user's source.
CHANNEL_SOURCE = "channel"
# Telegram allows 4096 characters per message; HTML tags count here, so stay below.
MAX_TEXT = 4000
_KEY = "announcement"


def participate_keyboard(bot_username: str) -> Keyboard:
    url = f"https://t.me/{bot_username}?start={CHANNEL_SOURCE}"
    return [[Button(t(CHANNEL_LANG, "channel_btn_participate"), url=url)]]


class Announcements:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        publisher: Publisher,
        *,
        bot_username: str,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sm = sessionmaker
        self.pub = publisher
        self.keyboard = participate_keyboard(bot_username)
        self.now = now

    async def text(self) -> str:
        """The admin's saved text, else the built-in default."""
        async with self.sm() as s:
            saved = await s.get(Setting, _KEY)
        return saved.value if saved else t(CHANNEL_LANG, "channel_announcement")

    async def set_text(self, html: str, *, admin_id: int) -> None:
        """Save the text for future announcement posts; published posts are left as they are."""
        if not html.strip() or len(html) > MAX_TEXT:
            raise ValueError(f"announcement text must be 1-{MAX_TEXT} characters")
        async with self.sm() as s, s.begin():
            await s.merge(Setting(key=_KEY, value=html, updated_at=self.now(), updated_by=admin_id))
            s.add(AuditLog(admin_id=admin_id, action="announce_text", payload={}, at=self.now()))

    async def publish(self, *, admin_id: int) -> int:
        """Post the announcement with the button. TelegramAPIError propagates."""
        msg_id = await self.pub.post_channel(await self.text(), self.keyboard)
        await self._audit(admin_id, "announce_publish", msg_id)
        return msg_id

    async def pin(self, msg_id: int, *, admin_id: int) -> None:
        """TelegramAPIError (e.g. missing rights) propagates."""
        await self.pub.pin(msg_id)
        await self._audit(admin_id, "announce_pin", msg_id)

    async def _audit(self, admin_id: int, action: str, msg_id: int) -> None:
        async with self.sm() as s, s.begin():
            s.add(
                AuditLog(
                    admin_id=admin_id, action=action, payload={"msg_id": msg_id}, at=self.now()
                )
            )
