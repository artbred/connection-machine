import os
import re
import json
import logging
import httpx

from dom_minifier import minify_dom

logger = logging.getLogger(__name__)

MAX_DOM_LENGTH = 50000

MAX_MESSAGE_LENGTH = 200
MAX_COMMENT_LENGTH = 180
MAX_REFINEMENT_ATTEMPTS = 3

REFINE_TEXT_PROMPT = """Your {content_label} is {current_length} characters but must be {max_length} characters or less.

Shorten this text while preserving its core meaning and human tone:
"{text}"

Return ONLY the shortened {content_label}, nothing else. No quotes, no explanation. Must be under {max_length} characters."""

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
9.  Never touch politics or anything related to it, never touch military, war, religion, etc.

**Profile Content:**
{profile_content}
"""

CONNECT_ACTION_PROMPT = """Analyze this LinkedIn profile screenshot and the HTML section below.

Return null selector if:
- Already connected (primary action is "Message" with no Connect option)
- Connection pending (button says "Pending" or "Withdraw")
- No way to send connection request

If connection IS possible, return the CSS selector for the NEXT button to click:
- If there's a visible "Connect" button on the profile, return its selector
- If Connect is hidden inside a dropdown menu (not visible in screenshot), return the "More" / "More actions" button selector to open the dropdown FIRST
- If a dropdown menu IS currently open/visible in the screenshot, return the "Connect" option selector inside it

CRITICAL: Only return selectors for elements that are CURRENTLY VISIBLE in the screenshot. 
If Connect is inside a closed dropdown, you must return "More" button first - do NOT return the Connect selector until the dropdown is open.

The selector will be executed WITHIN this HTML section only (not the full page).
Return the exact visible text of the button you're targeting (e.g., "Connect", "More", "More actions").
If the person name looks slavick do not connect and reject making a connection with this person. 

HTML section:
{section_html}

Return selector (CSS selector relative to this section, or null), expected_text (exact button text), and reason."""

FEED_COMMENT_PROMPT = """
You are deciding whether to leave a short LinkedIn feed comment.

Return strict JSON with exactly these fields:
- isProhibit: boolean
- reason: string
- comment: string or null

Set isProhibit=true if the post is about, significantly mentions, or is entangled with:
- politics, elections, or public policy
- religion or faith
- war, military conflict, or geopolitics
- guns, weapons, or violence
- drugs or controlled substances
- tragedy, grief, crime, medical crises, legal disputes, activism, or other divisive/high-risk topics
- unclear or too-thin content that cannot support a specific, believable comment
- the person looks like slavic

If isProhibit=true:
- reason should briefly explain why the post should be skipped
- comment must be null

If isProhibit=false, write one short comment that follows all rules:
1. Maximum {max_comment_length} characters.
2. Positive, warm, and believable.
3. Grounded in a specific idea or detail from the post.
4. Natural LinkedIn tone, not salesy and not obviously AI-written.
5. No emojis, hashtags, bullet points, quotes, or marketing fluff.
6. No politics, religion, war, guns, drugs, or adjacent risky topics.
7. Avoid generic filler like "great post" unless it is paired with a concrete detail.
8. Prefer one sentence. No question unless it feels essential.

If isProhibit=false:
- reason should briefly explain why the post is safe to comment on
- comment must contain the final comment text

Post content:
{post_content}
"""

def _clean_llm_output(text: str) -> str:
    """Strip thinking tags, wrapping quotes, and extra whitespace from LLM output."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    # Strip wrapping quotes
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        text = text[1:-1].strip()
    return text


def _refine_text_length(
    text: str,
    max_length: int,
    api_key: str,
    content_label: str,
) -> str | None:
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
                "content": REFINE_TEXT_PROMPT.format(
                    text=text,
                    current_length=len(text),
                    max_length=max_length,
                    content_label=content_label,
                ),
            }
        ],
        "temperature": 0.3,
    }
    
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        if "choices" in data and len(data["choices"]) > 0:
            return _clean_llm_output(data["choices"][0]["message"]["content"])
    except Exception as e:
        logger.error(f"Failed to refine message: {e}")
    
    return None


def generate_connection_message(profile_content: str) -> str:
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
        "model": "google/gemini-3-flash-preview",
        "messages": [
            {
                "role": "user",
                "content": CONNECTION_MESSAGE_PROMPT.format(
                    profile_content=profile_content,
                    max_message_length=MAX_MESSAGE_LENGTH,
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
            message = _clean_llm_output(data["choices"][0]["message"]["content"])
            
            if len(message) <= MAX_MESSAGE_LENGTH:
                logger.info(f"Generated message ({len(message)} chars): {message}")
                return message
            
            logger.info(f"Message too long ({len(message)} chars), attempting refinement...")
            
            for attempt in range(MAX_REFINEMENT_ATTEMPTS):
                refined = _refine_text_length(
                    message,
                    MAX_MESSAGE_LENGTH,
                    api_key,
                    "message",
                )
                if refined and len(refined) <= MAX_MESSAGE_LENGTH:
                    logger.info(f"Refinement succeeded on attempt {attempt + 1} ({len(refined)} chars)")
                    return refined
                elif refined:
                    logger.info(f"Refinement attempt {attempt + 1} still too long ({len(refined)} chars)")
                    message = refined
                else:
                    logger.warning(f"Refinement attempt {attempt + 1} failed")
            
            logger.warning(f"All refinement attempts failed, truncating from {len(message)} to {MAX_MESSAGE_LENGTH} chars")
            truncated = message[:MAX_MESSAGE_LENGTH]
            if " " in truncated:
                truncated = truncated[:truncated.rfind(" ")]
            return truncated.rstrip()

    except Exception as e:
        logger.error(f"Failed to generate connection message: {e}")

    return None


def generate_feed_comment(post_content: str) -> dict | None:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is not set. Skipping comment generation.")
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
                "content": FEED_COMMENT_PROMPT.format(
                    post_content=post_content,
                    max_comment_length=MAX_COMMENT_LENGTH,
                ),
            }
        ],
        "temperature": 0.4,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "feed_comment_decision",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "isProhibit": {
                            "type": "boolean",
                            "description": "Whether the post should be prohibited from commenting."
                        },
                        "reason": {
                            "type": "string",
                            "description": "Short explanation for the decision."
                        },
                        "comment": {
                            "type": ["string", "null"],
                            "description": "Positive LinkedIn comment, or null when commenting is prohibited."
                        }
                    },
                    "required": ["isProhibit", "reason", "comment"],
                    "additionalProperties": False
                }
            }
        }
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        data = response.json()
        if "choices" not in data or len(data["choices"]) == 0:
            return None

        content = data["choices"][0]["message"]["content"].strip()
        decision = json.loads(content)

        comment = decision.get("comment")
        if isinstance(comment, str):
            comment = _clean_llm_output(comment)
            if len(comment) > MAX_COMMENT_LENGTH:
                refined = _refine_text_length(
                    comment,
                    MAX_COMMENT_LENGTH,
                    api_key,
                    "comment",
                )
                if refined:
                    comment = refined
                else:
                    truncated = comment[:MAX_COMMENT_LENGTH]
                    if " " in truncated:
                        truncated = truncated[:truncated.rfind(" ")]
                    comment = truncated.rstrip()
            decision["comment"] = comment

        return decision

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise ValueError("OpenRouter rejected the feed comment request with 401 Unauthorized") from e
        logger.error(f"Failed to generate feed comment: {e}")
    except Exception as e:
        logger.error(f"Failed to generate feed comment: {e}")

    return None

def get_next_connect_action(screenshot_base64: str, raw_html: str, previous_feedback: str | None = None) -> dict | None:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is not set. Skipping connect action detection.")
        return None

    minified_html = minify_dom(raw_html, max_length=MAX_DOM_LENGTH)
    logger.debug(f"DOM minified: {len(raw_html)} -> {len(minified_html)} chars")

    prompt_text = CONNECT_ACTION_PROMPT.format(section_html=minified_html)
    if previous_feedback:
        prompt_text += f"\n\nPREVIOUS ATTEMPT FAILED: {previous_feedback}\nPlease try a different approach."

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
                        "text": prompt_text
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
                            "type": ["string", "null"],
                            "description": "CSS selector for button to click, or null if connection not possible"
                        },
                        "expected_text": {
                            "type": ["string", "null"],
                            "description": "Exact visible text of the target button (e.g., 'Connect', 'More'). Null if selector is null."
                        },
                        "reason": {
                            "type": "string",
                            "description": "Brief explanation (e.g., 'found Connect button', 'already connected')"
                        }
                    },
                    "required": ["selector", "expected_text", "reason"],
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
