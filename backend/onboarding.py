"""Shared starter questions and spoken onboarding guidance for both bindings."""

ONBOARDING_PLACEHOLDER = "{{ONBOARDING_GUIDANCE}}"

ONBOARDING_ANSWERS = {
    "What can you help me with?": (
        "I can help you explore meeting minutes and current public information about MTN. "
        "Ask about meeting discussions, decisions or action items. "
        "I can also look up company results, leadership and market news."
    ),
    "Tell me about your services": (
        "I can summarise meeting discussions and find recorded decisions, action items and attendees. "
        "I can also look up public results, leadership, share prices and industry news."
    ),
    "How do I get started?": (
        "Tap the microphone and speak, or type your question in the message box. "
        "Ask about a meeting or current public information. "
        "You can then ask follow-up questions for more detail."
    ),
}


def expand_onboarding(text: str) -> str:
    if ONBOARDING_PLACEHOLDER not in text:
        return text
    guidance = (
        "## Introducing this assistant\n\n"
        "When these questions or close paraphrases introduce the assistant, answer "
        "directly from the guidance below without calling a tool. They concern this "
        "assistant, not MTN's commercial services or an unrelated project. Do not "
        "ask what the user wants to get started with. Speak only the answer, never "
        "the question or a heading. Do not add capabilities or mention unsupported "
        "source categories. If a source or input method is unavailable in the "
        "current session, omit that claim rather than promise access.\n\n"
        + "\n\n".join(
            f'For "{question}", say:\n{answer}'
            for question, answer in ONBOARDING_ANSWERS.items()
        )
    )
    return text.replace(ONBOARDING_PLACEHOLDER, guidance)
