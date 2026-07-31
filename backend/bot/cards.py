"""Adaptive cards + Teams deep link for the bot (issues #53 / #28).

Keeps presentation (answer rendering, citation list, the "open the live avatar"
deep link into the channel B personal tab) separate from turn handling.
"""

from __future__ import annotations

import urllib.parse

from microsoft_agents.hosting.core import CardFactory

from ..config import TEAMS_APP_ID, TEAMS_TAB_ENTITY_ID
from .agent_runtime import AgentReply


def tab_deep_link(app_id: str = "", entity_id: str = "") -> str:
    """Build a Teams deep link that opens the personal static tab (#28).

    Format: ``https://teams.microsoft.com/l/entity/{appId}/{entityId}``. Returns
    an empty string when the app id is unknown so callers can omit the action.
    """
    app_id = (app_id or TEAMS_APP_ID).strip()
    entity_id = (entity_id or TEAMS_TAB_ENTITY_ID).strip()
    if not app_id or not entity_id:
        return ""
    label = urllib.parse.quote("Avatar")
    return (
        f"https://teams.microsoft.com/l/entity/{app_id}/{entity_id}"
        f"?label={label}"
    )


def answer_card(reply: AgentReply):
    """Render an agent answer as an Adaptive Card: text + sources + deep link.

    Returned as an attachment via ``CardFactory.adaptive_card`` so it can be
    attached to an outgoing activity.
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": reply.text,
            "wrap": True,
        }
    ]

    if reply.citations:
        body.append(
            {
                "type": "TextBlock",
                "text": "Sources",
                "weight": "Bolder",
                "spacing": "Medium",
                "size": "Small",
                "isSubtle": True,
            }
        )
        for i, c in enumerate(reply.citations, start=1):
            label = c.title or c.url or f"Source {i}"
            text = f"{i}. [{label}]({c.url})" if c.url else f"{i}. {label}"
            body.append(
                {
                    "type": "TextBlock",
                    "text": text,
                    "wrap": True,
                    "spacing": "Small",
                    "size": "Small",
                }
            )

    actions: list[dict] = []
    link = tab_deep_link()
    if link:
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "Open the live avatar",
                "url": link,
            }
        )

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return CardFactory.adaptive_card(card)


def format_text_reply(reply: AgentReply) -> str:
    """Plain-markdown fallback used where a card is not desirable.

    Teams renders a limited markdown subset; numbered citation links are safe.
    """
    parts = [reply.text]
    if reply.citations:
        parts.append("\n\n**Sources**")
        for i, c in enumerate(reply.citations, start=1):
            label = c.title or c.url or f"Source {i}"
            parts.append(f"\n{i}. [{label}]({c.url})" if c.url else f"\n{i}. {label}")
    link = tab_deep_link()
    if link:
        parts.append(f"\n\n[Open the live avatar]({link})")
    return "".join(parts)
