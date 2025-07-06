# ChatGPT Twitch Moderation Bot

An AI-powered moderation bot for Twitch chat that uses OpenAI's GPT models to detect and handle potential violations of Twitch's community guidelines in real-time.

## Features

- Real-time chat monitoring and moderation
- AI-powered content analysis using OpenAI's GPT models
- Configurable moderation thresholds based on user roles (mod, VIP, subscriber)
- Automatic message deletion for violations
- Escalation assistant runs in a separate thread to track users across threads and issue timeouts or bans without blocking regular moderation
- Detailed logging of chat and moderation actions
- Support for OAuth authentication with Twitch
- Batch processing to optimize API usage

## Requirements

- Python 3.7+
- OpenAI API key
- Twitch account with moderation privileges
- Twitch Developer Application credentials

## Installation

1. Clone the repository
2. Install dependencies:
```sh
pip install -r requirements.txt
```

3. Copy `config.yaml` and configure with your credentials:
```yaml
api_key: "your-openai-api-key"
assistant_id: "your-assistant-id"
escalation_assistant_id: "your-escalation-assistant-id"
model: "gpt-4o-mini"
batch_interval: 10
tokens_per_minute: 20000
moderation_timeout: 60
max_openai_content_size: 256000
max_rate_limit_retries: 3
twitch:
  server: "irc.chat.twitch.tv"
  port: 6697
  nickname: "YourBotName"
  client_id: "your-twitch-client-id"
  client_secret: "your-twitch-client-secret"
  channel: "#channel-to-moderate"
```

## Usage

Start the bot:
```sh
python bot.py
```

On first run, the bot will:
1. Generate required SSL certificates for OAuth
2. Open your browser for Twitch authorization
3. Begin monitoring chat after authorization

## Files

- `bot.py` - Main bot implementation
- `irc_client.py` - Twitch chat connection handling
- `moderation.py` - Message moderation logic
- `twitch_auth.py` - Twitch OAuth implementation
- `utils.py` - Helper utilities
- `prompt.txt` - AI moderation instructions

## Logging

The bot creates two log files:
- `chat_log.txt` - Chat messages and moderation actions
- `api_log.txt` - OpenAI API and HTTP request logs

## Configuration

See `config.yaml` for all configuration options. Key settings:

- `batch_interval`: Time in seconds between moderation batches
- `model`: OpenAI model to use for moderation
- `tokens_per_minute`: Rate limit for OpenAI API usage
- `moderation_timeout`: Timeout in seconds for each moderation batch
- `escalation_assistant_id`: Assistant used to analyze repeated offenses
- `max_openai_content_size`: Maximum JSON payload size sent to OpenAI
- `max_rate_limit_retries`: How many times to retry on rate limits
- Twitch credentials and connection settings

## Contributing

Feel free to submit issues and pull requests.

## License

This project includes a `LICENSE` file - please refer to it for terms of use.
