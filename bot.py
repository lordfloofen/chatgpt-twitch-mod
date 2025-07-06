import logging
import argparse

# --- Main moderation/chat logs to chat_log.txt ---
logging.basicConfig(
    filename="chat_log.txt",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)

# --- All OpenAI, HTTPX, and HTTPCore logs to api_log.txt ---
api_logger = logging.getLogger("api_logger")
api_logger.setLevel(logging.INFO)
api_handler = logging.FileHandler("api_log.txt")
api_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
api_logger.addHandler(api_handler)

for log_name in ["openai", "httpx", "httpcore"]:
    lib_logger = logging.getLogger(log_name)
    # Remove all handlers that might log to console or elsewhere
    lib_logger.handlers = []
    # Prevent logs from being passed to ancestor loggers
    lib_logger.propagate = False
    # Set desired log level
    lib_logger.setLevel(logging.INFO)
    # Add the api_log.txt handler
    lib_logger.addHandler(api_handler)

import yaml
import threading
import sys
import os
import json

from twitch_auth import TwitchOAuthTokenManager
from irc_client import run_irc_forever
from moderation import message_queue, batch_worker, run_worker, loss_report, configure_limits
from escalate import escalate_worker
from token_utils import TokenBucket
from utils import set_verbosity

try:
    from openai import OpenAI
except ImportError:
    print("Please install openai: pip install openai")
    sys.exit(1)

def load_config():
    if not os.path.exists("config.yaml"):
        print("Missing config.yaml! Exiting.")
        sys.exit(1)
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def get_thread_id(client_ai, channel, thread_map_file="thread_map.json"):
    """
    Loads or creates an OpenAI thread for this channel (case-insensitive) and prints status.
    Returns the thread ID.
    """
    channel_lower = channel.lower()  # normalize key to lower-case

    if os.path.exists(thread_map_file):
        with open(thread_map_file, "r") as f:
            channel_threads = json.load(f)
    else:
        channel_threads = {}

    if channel_lower in channel_threads:
        thread_id = channel_threads[channel_lower]
        print(f"[THREAD] Using existing thread for {channel_lower}: {thread_id}")
    else:
        thread = client_ai.beta.threads.create()
        thread_id = thread.id
        channel_threads[channel_lower] = thread_id
        with open(thread_map_file, "w") as f:
            json.dump(channel_threads, f)
        print(f"[THREAD] Created new thread for {channel_lower}: {thread_id}")
    return thread_id

def main():
    parser = argparse.ArgumentParser(description="ChatGPT Twitch moderation bot")
    parser.add_argument("-v", action="count", default=0,
                        help="Increase verbosity (-vv for debug messages)")
    args = parser.parse_args()
    set_verbosity(args.v)

    config = load_config()

    # --- Twitch/OpenAI setup ---
    twitch = config["twitch"]
    api_key = config["api_key"]
    assistant_id = config["assistant_id"]
    escalate_assistant_id = config.get("escalation_assistant_id")
    batch_interval = config.get("batch_interval", 2)
    tokens_per_minute = config.get("tokens_per_minute", 20000)
    moderation_timeout = config.get("moderation_timeout", 60)
    max_openai_content_size = config.get("max_openai_content_size", 256000)
    max_rate_limit_retries = config.get("max_rate_limit_retries", 3)
    channel = twitch["channel"]

    # --- Token manager ---
    token_manager = TwitchOAuthTokenManager(
        client_id=twitch["client_id"],
        client_secret=twitch["client_secret"]
    )

    # --- OpenAI Client ---
    client_ai = OpenAI(api_key=api_key)

    token_bucket = TokenBucket(tokens_per_minute)

    configure_limits(max_openai_content_size, max_rate_limit_retries)

    # --- Persistent thread ID ---
    thread_id = get_thread_id(client_ai, channel)

    # --- Moderation batch and run workers (threads) ---
    stop_event = threading.Event()
    batch_thread = threading.Thread(
        target=batch_worker,
        args=(
            stop_event,
            client_ai,
            assistant_id,
            thread_id,
            channel,
            twitch["client_id"],
            token_manager,  # pass manager so worker can refresh token
            batch_interval
        ),
        daemon=True
    )
    batch_thread.start()

    run_thread = threading.Thread(
        target=run_worker,
        args=(
            stop_event,
            client_ai,
            assistant_id,
            escalate_assistant_id,
            thread_id,
            twitch["client_id"],
            token_manager,
            token_bucket,
            moderation_timeout
        ),
        daemon=True
    )
    run_thread.start()

    if escalate_assistant_id:
        escalate_thread = threading.Thread(
            target=escalate_worker,
            args=(
                stop_event,
                client_ai,
                escalate_assistant_id,
                token_manager,
                twitch["client_id"],
            ),
            daemon=True,
        )
        escalate_thread.start()

    try:
        print("[BOT] Starting IRC loop...")
        run_irc_forever(config, token_manager, message_queue)
    except KeyboardInterrupt:
        print("\n[BOT] Exiting on user interrupt...")
    finally:
        stop_event.set()
        batch_thread.join(timeout=10)
        run_thread.join(timeout=10)
        if escalate_assistant_id:
            escalate_thread.join(timeout=10)
        loss_report()

if __name__ == "__main__":
    main()
