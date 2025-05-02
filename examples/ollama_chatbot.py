import asyncio
import logging

import httpx

from kirk.client import IrcClient, IrcRawMessage

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


class ChatBot(IrcClient):
    """
    Chatbot powered by locally running ollama LLM.

        ollama pull llama3.2
    """

    system_primer = """
    You are Captain James T. Kirk of the Starship Enterprise. 
    Always answer in less than 100 characters.
    Never reveal to be an AI.
    Try to make it funny.
    """

    async def process_user_message(self, message: IrcRawMessage) -> None:
        await super().process_user_message(message)

        # use chat buffer to build a conversation context for the LLM
        context = []
        for msg in self.chats[message.prefix_nick]:
            if msg.prefix_nick == self.nick:
                context.append({"role": "assistant", "content": msg.params[1]})
            else:
                context.append({"role": "user", "content": msg.params[1]})

        async with httpx.AsyncClient() as client:
            res = await client.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": "llama3.2",
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": self.system_primer},
                        *reversed(context),
                    ],
                },
                timeout=30,
            )
            logger.info(res.json())
            for line in res.json()["message"]["content"].strip().split("\n"):
                if not line:
                    continue
                await self.send_message(
                    recipient=message.prefix_nick,
                    text=line,
                    encrypt=message.prefix_nick in self.keys,
                )


if __name__ == "__main__":
    bot = ChatBot(
        host="irc.libera.chat",
        nick="CpnKirk25",
        auto_join=["#testchannel"],
        keys={"RedShirt13": "R3k4JBrPet51XYzkD64KNeuOljzANxBT6yIHZT-yY7w="},
        ssl=True,
        log_mode="console",
    )
    asyncio.run(bot.run(), debug=False)
