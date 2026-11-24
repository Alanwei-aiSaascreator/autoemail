from autoemail.channels.base import NormalizedMessage


def make_message(
    *,
    dedupe: str = "hash-1",
    message_id: str | None = "m1@example.com",
    subject: str = "Re: bond",
    body: str = "hello",
    from_addr: str = "toni@a1surety.com",
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    to_addrs: list[str] | None = None,
) -> NormalizedMessage:
    return NormalizedMessage(
        channel="eml",
        channel_msg_id=message_id,
        from_addr=from_addr,
        subject=subject,
        body_text=body,
        in_reply_to=in_reply_to,
        references=references or [],
        to_addrs=to_addrs or ["ops@example.com"],
        dedupe_hash=dedupe,
    )
