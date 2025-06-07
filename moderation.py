import threading
import time
import json
import logging
from queue import SimpleQueue
from datetime import datetime
import concurrent.futures
import requests

# --- Message queue used for incoming messages from IRC ---
message_queue = SimpleQueue()

# --- For loss detection/reporting ---
produced_ids = set()   # Track all messages received
consumed_ids = set()   # Track all messages processed

MAX_OPENAI_CONTENT_SIZE = 256000  # OpenAI's max content field length in characters
MODERATION_TIMEOUT_SECONDS = 60   # Max time to allow for an API moderation call

def delete_chat_message(broadcaster_id, moderator_id, message_id, token, client_id):
    url = "https://api.twitch.tv/helix/moderation/chat"
    headers = {
        'Client-ID': client_id,
        'Authorization': f'Bearer {token}',
    }
    params = {
        'broadcaster_id': broadcaster_id,
        'moderator_id': moderator_id,
        'message_id': message_id
    }
    response = requests.delete(url, headers=headers, params=params, timeout=10)
    if response.status_code == 204:
        print(f"[TWITCH] Deleted message {message_id}")
        return True
    else:
        print(f"[TWITCH][ERROR] Failed to delete message {message_id}: {response.status_code} - {response.text}")
        return False

def moderate_batch(openai_client, assistant_id, model, thread_id, batch, channel_info=None, token=None, client_id=None):
    """
    Sends a batch of messages to the OpenAI Assistant API for moderation.
    Only marks messages as consumed if API call succeeds.
    If the batch is too large, splits it and retries.
    Deletes flagged messages from Twitch chat.
    Returns True on success, False on (timeout or API error).
    """
    try:
        context = {
            "game": channel_info.get('stream', {}).get('game_name') if channel_info else None,
            "channel_info": channel_info
        } if channel_info else {}

        payload = {
            "context": context,
            "messages": batch
        }
        batch_json = json.dumps(payload)
        if len(batch_json) > MAX_OPENAI_CONTENT_SIZE:
            print(f"[ERROR][MODERATION] Batch too large ({len(batch_json)} chars), splitting and retrying.")
            # Prevent infinite recursion: if batch of 1 is still too big, drop and log it
            if len(batch) == 1:
                print(f"[FATAL][MODERATION] Single message too large to send, skipping: {batch[0]['id']}")
                return False
            mid = len(batch) // 2
            left = moderate_batch(openai_client, assistant_id, model, thread_id, batch[:mid], channel_info, token, client_id)
            right = moderate_batch(openai_client, assistant_id, model, thread_id, batch[mid:], channel_info, token, client_id)
            return left and right

        resp_msg = openai_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=batch_json
        )

        run = openai_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
            model=model
        )

        while True:
            status = openai_client.beta.threads.runs.retrieve(run.id, thread_id=thread_id)
            if status.status == "completed":
                break
            time.sleep(0.2)

        result = openai_client.beta.threads.messages.list(thread_id=thread_id)
        latest = result.data[0].content[0].text.value if result.data else ""
        print(f"[MODERATION]\n{latest}\n{'='*40}")
        if latest:
            logging.info(f"[MODERATION RESULT] {latest}")
            # --- Parse and delete flagged messages ---
            try:
                # Your output is a JSON array (list of dicts)
                flagged = json.loads(latest)
                if isinstance(flagged, list):
                    # Get channel info IDs
                    broadcaster_id = channel_info['user']['id']
                    moderator_id = channel_info['user']['id']  # change if your bot is a moderator but not the owner!
                    for msg in flagged:
                        msg_id = msg.get("id")
                        if msg_id:
                            delete_chat_message(
                                broadcaster_id,
                                moderator_id,
                                msg_id,
                                token,
                                client_id
                            )
            except Exception as e:
                print(f"[ERROR][MODERATION][DELETE] Failed to parse/delete: {e}")

        # Only here is success, so now mark as consumed
        for msg in batch:
            consumed_ids.add(msg["id"])
        return True

    except Exception as e:
        print(f"[ERROR][MODERATION] {e}")
        return False

def moderate_batch_with_timeout(openai_client, assistant_id, model, thread_id, batch, channel_info=None, token=None, client_id=None):
    """
    Runs moderate_batch in a separate thread, enforcing a timeout.
    If timeout/error, logs and does not mark messages as consumed.
    Returns True on success, False on fail/timeout.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            moderate_batch,
            openai_client, assistant_id, model, thread_id, batch, channel_info, token, client_id
        )
        try:
            return future.result(timeout=MODERATION_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            print(f"[TIMEOUT][MODERATION] Moderation batch took longer than {MODERATION_TIMEOUT_SECONDS} seconds. Skipping this batch (not marking as consumed).")
            logging.warning(f"[TIMEOUT][MODERATION] Moderation batch timed out. Batch size: {len(batch)}")
            return False

def get_channel_info(channel, client_id, token):
    """
    Fetches Twitch channel/game/stream metadata via the Helix API.
    Returns a dict or None on error.
    """
    import requests
    user_login = channel.lstrip('#')
    headers = {
        'Client-ID': client_id,
        'Authorization': f'Bearer {token}',
    }
    info = {}
    # User info
    user_url = f'https://api.twitch.tv/helix/users?login={user_login}'
    user_resp = requests.get(user_url, headers=headers, timeout=10)
    if user_resp.status_code != 200 or not user_resp.json().get('data'):
        return None
    user_data = user_resp.json()['data'][0]
    info['user'] = user_data
    user_id = user_data['id']

    # Stream info
    stream_url = f'https://api.twitch.tv/helix/streams?user_id={user_id}'
    stream_resp = requests.get(stream_url, headers=headers, timeout=10)
    stream_data = (stream_resp.json()['data'][0]
                  if stream_resp.status_code == 200 and stream_resp.json().get('data') else {})
    info['stream'] = stream_data

    # Channel info
    chan_url = f'https://api.twitch.tv/helix/channels?broadcaster_id={user_id}'
    chan_resp = requests.get(chan_url, headers=headers, timeout=10)
    chan_data = (chan_resp.json()['data'][0]
                 if chan_resp.status_code == 200 and chan_resp.json().get('data') else {})
    info['channel'] = chan_data

    # Tags
    tags_url = f'https://api.twitch.tv/helix/tags/streams?broadcaster_id={user_id}'
    tags_resp = requests.get(tags_url, headers=headers, timeout=10)
    tags = (tags_resp.json().get('data')
            if tags_resp.status_code == 200 and tags_resp.json().get('data') else [])
    info['tags'] = tags

    # Followers count
    follows_url = f'https://api.twitch.tv/helix/users/follows?to_id={user_id}&first=1'
    follows_resp = requests.get(follows_url, headers=headers, timeout=10)
    follows_count = follows_resp.json().get('total', 0) if follows_resp.status_code == 200 else 0
    info['followers'] = follows_count

    return info

def batch_worker(stop_event, openai_client, assistant_id, model, thread_id, channel, client_id, token, batch_interval=2):
    """
    Worker that batches messages for moderation and sends them to OpenAI.
    Should be run in a background thread.
    """
    batch = []
    last_send = time.time()

    channel_info = None
    last_channel_info_time = 0
    CHANNEL_INFO_REFRESH = 60  # seconds

    while not stop_event.is_set() or not message_queue.empty():
        try:
            # Drain queue quickly, up to big batch
            while not message_queue.empty() and len(batch) < 500:
                msg = message_queue.get()
                batch.append(msg)
                produced_ids.add(msg["id"])

            now = time.time()
            # Refresh channel info every 60s
            if now - last_channel_info_time > CHANNEL_INFO_REFRESH or channel_info is None:
                try:
                    channel_info = get_channel_info(channel, client_id, token)
                    last_channel_info_time = now
                except Exception as e:
                    print(f"[WARN] Could not fetch channel info: {e}")
                    channel_info = None

            if batch and (now - last_send >= batch_interval):
                print(f"[INFO] Sending batch of {len(batch)} messages to moderation...")
                ok = moderate_batch_with_timeout(
                    openai_client, assistant_id, model, thread_id, batch, channel_info, token, client_id
                )
                if not ok:
                    print(f"[ERROR][BATCH] Moderation failed or timed out. Batch of {len(batch)} messages was NOT marked as consumed.")
                    # Optionally: write batch to a "failed" queue or file for later replay
                batch.clear()
                last_send = now
            time.sleep(0.05)
        except Exception as e:
            print(f"[ERROR][BATCH] {e}")

    # Final flush
    if batch:
        print(f"[INFO] Final flush of {len(batch)} messages...")
        ok = moderate_batch_with_timeout(
            openai_client, assistant_id, model, thread_id, batch, channel_info, token, client_id
        )
        if not ok:
            print(f"[ERROR][BATCH] Final moderation failed or timed out. Last batch of {len(batch)} messages was NOT marked as consumed.")

def loss_report():
    """
    Prints how many messages were produced vs. consumed by moderation pipeline.
    """
    missing = produced_ids - consumed_ids
    print(f"\n[LOSS DETECTION]")
    print(f"  Total messages produced: {len(produced_ids)}")
    print(f"  Total messages consumed: {len(consumed_ids)}")
    print(f"  Total missing: {len(missing)}")
    if missing:
        print(f"  Missing message IDs: {list(missing)[:10]}{' ...' if len(missing) > 10 else ''}")
