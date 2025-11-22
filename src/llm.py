import os
import logging
import httpx

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENTGH = 200

# Prompt for generating connection messages
CONNECTION_MESSAGE_PROMPT = """
You are a professional networking assistant.
Generate a personalized LinkedIn connection message (maximum {max_message_length} characters) based on the provided profile content.
The message should be polite, professional, and mention specific details from the user's experience or summary to show genuine interest.
Do not include placeholders like "[Your Name]" - write it as a template ready to send or generic enough.
Focus on finding common ground or appreciating their work.

Profile Content:
{profile_content}
"""


def generate_connection_message(profile_content: str) -> str:
    """
    Generates a personalized LinkedIn connection message using OpenRouter.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is not set. Skipping message generation.")
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "LinkedIn Auto-Connector",
    }

    payload = {
        "model": "z-ai/glm-4.6",
        "messages": [
            {
                "role": "user",
                "content": CONNECTION_MESSAGE_PROMPT.format(
                    profile_content=profile_content,
                    max_message_length=MAX_MESSAGE_LENTGH,
                ),
            }
        ],
        "temperature": 0.6,
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]["content"].strip()
            if len(message) > MAX_MESSAGE_LENTGH:
                logger.warning("Generated message is too long, truncating...")
                message = message[: MAX_MESSAGE_LENTGH - 3] + "..."
            return message

    except Exception as e:
        logger.error(f"Failed to generate connection message: {e}")

    return None
