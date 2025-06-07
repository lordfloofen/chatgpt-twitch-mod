import yaml
import logging
import threading
import sys
import os
import json

from twitch_auth import TwitchOAuthTokenManager
from irc_client import run_irc_forever
from moderation import message_queue, batch_worker, loss_report

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
    Loads or creates an OpenAI thread for this channel and prints status.
    Returns the thread ID.
    """
    if os.path.exists(thread_map_file):
        with open(thread_map_file, "r") as f:
            channel_threads = json.load(f)
    else:
        channel_threads = {}

    if channel in channel_threads:
        thread_id = channel_threads[channel]
        print(f"[THREAD] Using existing thread for {channel}: {thread_id}")
    else:
        thread = client_ai.beta.threads.create()
        thread_id = thread.id
        channel_threads[channel] = thread_id
        with open(thread_map_file, "w") as f:
            json.dump(channel_threads, f)
        print(f"[THREAD] Created new thread for {channel}: {thread_id}")
    return thread_id

def main():
    config = load_config()
    logging.basicConfig(
        filename="chat_log.txt",
        level=logging.INFO,
        format="%(asctime)s %(message)s",
    )

    # --- Twitch/OpenAI setup ---
    twitch = config["twitch"]
    api_key = config["api_key"]
    assistant_id = config["assistant_id"]
    model = config.get("model", "gpt-4o-mini")
    batch_interval = config.get("batch_interval", 2)
    channel = twitch["channel"]

    # --- Token manager ---
    token_manager = TwitchOAuthTokenManager(
        client_id=twitch["client_id"],
        client_secret=twitch["client_secret"]
    )

    # --- OpenAI Client ---
    client_ai = OpenAI(api_key=api_key)

    # --- Persistent thread ID ---
    thread_id = get_thread_id(client_ai, channel)

    # --- Moderation batch worker (thread) ---
    stop_event = threading.Event()
    batch_thread = threading.Thread(
        target=batch_worker,
        args=(
            stop_event,
            client_ai,
            assistant_id,
            model,
            thread_id,
            channel,
            twitch["client_id"],
            token_manager.get_token(),  # Pass OAuth token for message deletion
            batch_interval
        ),
        daemon=True
    )
    batch_thread.start()

    try:
        print("[BOT] Starting IRC loop...")
        run_irc_forever(config, token_manager, message_queue)
    except KeyboardInterrupt:
        print("\n[BOT] Exiting on user interrupt...")
    finally:
        stop_event.set()
        batch_thread.join(timeout=10)
        loss_report()

if __name__ == "__main__":
    main()
