"""Honest account kind on the wire — human vs agent (#3096, increment 1).

The acceptance criterion this covers: "member lists / message payloads carry the
agent kind honestly (no human badge)". Grounded on the app tab's ADR-0005, which
makes a bot a first-class Principal of its own rather than a flag on its creator's
identity, so an agent must be able to SAY it is an agent.

Why these tests are worth their weight: before #3096 both writers of
``sender_kind`` hardcoded the literal ``"human"`` for any identified sender —
``create_outbound`` (the authenticated send path an agent will actually use) and
``_kind_for``'s identified arm. Neither consulted the account. So the first agent
to hold a User row would have posted under a human badge on every message, and
nothing in the suite would have noticed, because no test asserted *why*
sender_kind was 'human' — only that it was.

Each test below therefore pins the CAUSE (the sender's own kind), not the value.
Delete the ``user.kind`` reads in messages_service and these go red; that is the
RED proof they are load-bearing.
"""
from __future__ import annotations

import pytest

from aiko_gateway.domain import messages_service
from aiko_gateway.domain.models import Channel, User, UserKind


def _channel(kind: str = "standard") -> Channel:
    return Channel(id="0" * 26, name="general", kind=kind, aiko_channel="general")


def _user(kind: str, uid: str = "u" * 26, name: str = "ada") -> User:
    return User(id=uid, username=name, display_name=name.title(),
                aiko_username=name, kind=kind)


@pytest.mark.asyncio
async def test_agent_send_is_stamped_agent_not_human(session):
    """The regression that motivated the whole increment."""
    channel, agent = _channel(), _user(UserKind.AGENT, name="armbot")
    session.add_all([channel, agent])
    await session.commit()

    row, created = await messages_service.create_outbound(
        session, user=agent, channel=channel, body="arm online", client_msg_id="m1")
    assert created
    assert row.sender_kind == "agent"
    # ...and it reaches the CLIENT, not just the row. message_view is the payload
    # the app renders a badge from, so a truthful column with a lying view would
    # be the same bug one layer out.
    assert messages_service.message_view(row)["sender"]["kind"] == "agent"


@pytest.mark.asyncio
async def test_human_send_still_stamped_human(session):
    """The control. Without it, a change that stamped everything 'agent' would
    pass the test above and still be completely wrong."""
    channel, human = _channel(), _user(UserKind.HUMAN)
    session.add_all([channel, human])
    await session.commit()

    row, _ = await messages_service.create_outbound(
        session, user=human, channel=channel, body="hi", client_msg_id="m1")
    assert row.sender_kind == "human"


@pytest.mark.asyncio
async def test_user_kind_defaults_to_human(session):
    """A User constructed without a kind is a human — every account that predates
    #3096, and every ordinary registration path that has not been taught about
    kinds, must keep working and must not silently become an agent."""
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add(user)
    await session.commit()
    assert user.kind == "human"


@pytest.mark.asyncio
async def test_sender_kind_prefers_the_account_over_the_channel(session):
    """A HUMAN posting in a 'robot' channel is still a human (#3096).

    This pins the direction of the fix. ``_kind_for``'s unidentified-sender arm
    falls back to the channel's kind, which answers "where was this sent" rather
    than "who sent it". That fallback is correct ONLY when there is no account to
    ask; the moment there is one, the account wins. Getting this backwards would
    badge every human in a robot channel as a robot.
    """
    channel, human = _channel(kind="robot"), _user(UserKind.HUMAN)
    assert messages_service._kind_for(channel, human) == "human"
    agent = _user(UserKind.AGENT, uid="a" * 26, name="armbot")
    assert messages_service._kind_for(channel, agent) == "agent"
    # No account to ask -> the channel is the only signal left.
    assert messages_service._kind_for(channel, None) == "robot"
