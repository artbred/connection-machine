# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TREL is a LinkedIn automation tool that sends connection requests with AI-personalized messages and creates posts. It uses Playwright for browser automation with anti-detection measures, PostgreSQL for task queueing, and OpenRouter API for message generation.

## Development Commands

```bash
# Install dependencies (using uv package manager)
uv sync

# Run the application
python src/main.py

# Debug mode - run single invite without database
python src/main.py --debug-invite "https://linkedin.com/in/profile-url"
python src/main.py --debug-invite "https://linkedin.com/in/profile-url" --no-message

# Populate database with LinkedIn URLs from CSV
python utils/populate_db.py

# Docker
docker-compose up -d
docker-compose logs -f trel
```

## Architecture

```
main.py (Entry point + auth)
    ↓
TaskDispatcher (Polling loop, rate limiting)
    ├── InviteTask → Sends connection requests
    └── PostTask → Creates LinkedIn posts
    ↓
Database (PostgreSQL via SQLAlchemy)
LLM (OpenRouter API for message generation)
HumanActions (Bot evasion via simulated human behavior)
```

### Key Components

- **dispatcher.py** - Task orchestration with rate limiting (15 invites/day, 50 posts/day), randomized spacing between tasks, zombie task cleanup
- **tasks/invite.py** - Connection request workflow: profile extraction, LLM-powered connect button detection, personalized message insertion
- **tasks/post.py** - Post creation with human-like typing delays
- **llm.py** - OpenRouter integration using Qwen3-Next for messages, Claude Haiku for selector detection
- **human_actions.py** - Anti-detection: Bezier curve mouse movement, Gaussian click distribution, randomized typing delays
- **db.py** - SQLAlchemy models for Task (type, status, payload, timestamps)

### Task Flow

1. Poll database every 10 seconds for PENDING tasks (FIFO)
2. Check rate limits per task type
3. Set status → PROCESSING, execute handler
4. On success: COMPLETED + schedule next with randomized spacing
5. On failure: FAILED + record error
6. On session expiration: Reset to PENDING, re-authenticate

## Environment Variables

Required in `.env`:
- `LINKEDIN_USERNAME`, `LINKEDIN_PASSWORD` - LinkedIn credentials
- `DATABASE_URL` - PostgreSQL connection string
- `OPENROUTER_API_KEY` - For AI message generation
- `HEADLESS` - Browser visibility (true/false)
- `SOCKS_PROXY` - Optional proxy (socks5://host:port)
- `TELEGRAM_NOTIFICATIONS_URL`, `TELEGRAM_CHAT_ID`, `TELEGRAM_API_KEY` - Notification config

## Browser Automation Stack

Uses Patchright (stealth Playwright variant) + playwright-stealth for anti-detection. Browser runs with persistent context to preserve authentication. Remote debugging enabled on port 9224.
