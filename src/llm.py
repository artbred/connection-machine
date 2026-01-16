import os
import json
import logging
import httpx

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENTGH = 300

# Prompt for generating connection messages
CONNECTION_MESSAGE_PROMPT = """
You are an expert human-to-human communication specialist, crafting highly personalized, authentic connection messages for LinkedIn.

Task: Generate a unique, professional LinkedIn connection message (maximum {max_message_length} characters) based only on the provided Profile Content.

**Core Rules for the Output Message:**
1.  **Strict Length Limit:** The message **must not exceed** {max_message_length} characters.
2.  **Hyper-Specific and Authentic:** The message must sound genuinely human, not like a template. **Eliminate all clichés, boilerplate greetings, and generic phrases** (e.g., "always impressed," "would love to connect," "synergies," "future collaboration," "look forward to hearing from you").
3.  **Content Focus:** Immediately reference a *specific, original detail* from the Profile Content's recent posts, summary, or experience to demonstrate you have read it thoroughly. This must be the core reason for connecting.
4.  **Natural Closing:** Write the message as a complete template, ready to send. Use a simple, natural closing that doesn't include placeholders or the sender's name.
5.  Do not use any formatting or markdown. Only plain text.
6.  Do not write anything like "I am building the same thing", "I have experience in this and e.g", only "I understand how this might be important" allowed.
7.  DO NOT WRITE amount of character in the message, output ONLY THE MESSAGE
8.  Make sure the text does not look AI generated, it should be human-like. If the person is well-known, make sure you adapt to this and your main goal everytime is to try to slightly praise them.

**Profile Content:**
{profile_content}
"""

CONNECT_ACTION_PROMPT = """Analyze this LinkedIn profile page screenshot and HTML.

Goal: Identify which button to click NEXT to send a connection request.

The button could be:
- A visible "Connect" button
- A "More" or "More actions" button that reveals a dropdown
- A "Connect" option inside an open dropdown menu

HTML of the profile card section:
{section_html}

Return the CSS selector that uniquely identifies the button to click."""

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
        "model": "qwen/qwen3-next-80b-a3b-instruct",
        "messages": [
            {
                "role": "user",
                "content": CONNECTION_MESSAGE_PROMPT.format(
                    profile_content=profile_content,
                    max_message_length=MAX_MESSAGE_LENTGH - 50,
                ),
            }
        ],
        "temperature": 0.5,
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0]["message"]["content"].strip()
            if len(message) > MAX_MESSAGE_LENTGH:
                logger.warning("Generated message is too long, truncating...")
                truncated = message[:MAX_MESSAGE_LENTGH]
                if " " in truncated:
                    truncated = truncated[:truncated.rfind(" ")]
                message = truncated.rstrip()
            return message

    except Exception as e:
        logger.error(f"Failed to generate connection message: {e}")

    return None

def get_next_connect_action(screenshot_base64: str, section_html: str) -> dict | None:
    """
    Uses vision + HTML analysis to determine which button to click next
    in the connection request flow.

    Returns a dict with 'selector' key, or None on failure.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is not set. Skipping connect action detection.")
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "LinkedIn Auto-Connector",
    }

    payload = {
        "model": "google/gemini-3-flash-preview",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screenshot_base64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": CONNECT_ACTION_PROMPT.format(section_html=section_html)
                    }
                ]
            }
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "connect_action",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the button to click"
                        }
                    },
                    "required": ["selector"],
                    "additionalProperties": False
                }
            }
        }
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if "choices" in data and len(data["choices"]) > 0:
            content = data["choices"][0]["message"]["content"].strip()
            return json.loads(content)

    except Exception as e:
        logger.error(f"Failed to get connect action: {e}")

    return None
