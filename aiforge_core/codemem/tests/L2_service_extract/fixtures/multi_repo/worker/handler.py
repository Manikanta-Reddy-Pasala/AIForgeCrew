"""worker/handler.py — message processing logic."""


async def handle_message(msg) -> None:
    print(f"got: {msg.subject} {msg.data!r}")
    await msg.ack()
