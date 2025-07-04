import threading
import time
import json
import logging
import queue
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import requests

# --- Message queue used for incoming messages from IRC ---
message_queue = queue.Queue()
run_queue = queue.Queue()

produced_ids = set()
consumed_ids = set()
not_moderated = set()

MAX_OPENAI_CONTENT_SIZE = 256000
MODERATION_TIMEOUT_SECONDS = 60

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
    try:
        response = requests.delete(url, headers=headers, params=params, timeout=10)
        if response.status_code == 204:
            print(f"[TWITCH] Deleted message {message_id}")
            return True
        else:
            print(f"[TWITCH][ERROR] Failed to delete message {message_id}: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"[TWITCH][ERROR] Exception deleting message {message_id}: {e}")
        return False

def wait_for_runs_to_complete(openai_client, thread_id, poll_interval=5, timeout=120):
    start = time.time()
    while True:
        try:
            runs = openai_client.beta.threads.runs.list(thread_id=thread_id).data
            active = [r for r in runs if r.status not in ("completed", "failed", "cancelled", "expired")]
            if not active:
                return
            if time.time() - start > timeout:
                print(f"[ERROR][RUN] Timed out waiting for runs to complete on thread {thread_id}")
                return
            time.sleep(poll_interval)
        except Exception as e:
            print(f"[ERROR][RUN] Exception while waiting for runs: {e}")
            return

def wait_for_run_completion(openai_client, thread_id, run, poll_interval=5, timeout=120):
    """Wait for a single run to complete and return its final status."""
    start = time.time()

    # Use the official wait() helper if available
    try:
        wait_fn = getattr(openai_client.beta.threads.runs, "wait", None)
        if callable(wait_fn):
            return wait_fn(run_id=run.id, thread_id=thread_id, timeout=timeout)
    except Exception as e:
        print(f"[WARN][RUN] wait() helper failed: {e}; falling back to manual polling")

    while True:
        try:
            status = openai_client.beta.threads.runs.retrieve(run.id, thread_id=thread_id)
            if status.status == "completed":
                return status
            if status.status in ("failed", "cancelled", "expired"):
                print(f"[ERROR][MODERATION] Run status for {run.id} is {status.status}")
                return status
            if time.time() - start > timeout:
                print(f"[ERROR][RUN] Timed out waiting for run {run.id} on thread {thread_id}")
                return status
            time.sleep(poll_interval)
        except Exception as e:
            print(f"[ERROR][RUN] Exception while waiting for run: {e}")
            return None

def run_with_timeout(func, args=(), kwargs=None, timeout=60):
    if kwargs is None:
        kwargs = {}
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            print(f"[TIMEOUT][MODERATION] Moderation batch took longer than {timeout} seconds. Skipping this batch (not marking as consumed).")
            return False
        except Exception as e:
            print(f"[ERROR][MODERATION][THREAD] {e}")
            return False

def moderate_batch(openai_client, assistant_id, model, thread_id, batch, channel_info=None, token=None, client_id=None):
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
            if len(batch) == 1:
                print(f"[FATAL][MODERATION] Single message too large to send, skipping: {batch[0]['id']}")
                return False
            mid = len(batch) // 2
            left = moderate_batch(openai_client, assistant_id, model, thread_id, batch[:mid], channel_info, token, client_id)
            right = moderate_batch(openai_client, assistant_id, model, thread_id, batch[mid:], channel_info, token, client_id)
            return left and right

        wait_for_runs_to_complete(openai_client, thread_id, poll_interval=5)

        try:
            resp_msg = openai_client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=batch_json
            )
        except Exception as e:
            print(f"[ERROR][MODERATION] Exception adding messages: {e}")
            return False

        try:
            run = openai_client.beta.threads.runs.create(
                thread_id=thread_id,
                assistant_id=assistant_id,
                model=model
            )
        except Exception as e:
            print(f"[ERROR][MODERATION] Exception creating run: {e}")
            return False

        status = wait_for_run_completion(openai_client, thread_id, run, poll_interval=5)
        if not status or status.status != "completed":
            return False

        try:
            result = openai_client.beta.threads.messages.list(thread_id=thread_id)
            latest = result.data[0].content[0].text.value if result.data else ""
        except Exception as e:
            print(f"[ERROR][MODERATION] Exception getting results: {e}")
            latest = ""

        print(f"[MODERATION]\n{latest}\n{'='*40}")
        if latest:
            logging.info(f"[MODERATION RESULT] {latest}")
            try:
                flagged = json.loads(latest)
                if isinstance(flagged, list) and flagged:
                    broadcaster_id = channel_info['user']['id']
                    moderator_id = channel_info['user']['id']
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

        for msg in batch:
            consumed_ids.add(msg["id"])
        return True

    except Exception as e:
        print(f"[ERROR][MODERATION][UNHANDLED] {e}")
        return False

def get_channel_info(channel, client_id, token):
    import requests
    user_login = channel.lstrip('#')
    headers = {
        'Client-ID': client_id,
        'Authorization': f'Bearer {token}',
    }
    info = {}
    try:
        user_url = f'https://api.twitch.tv/helix/users?login={user_login}'
        user_resp = requests.get(user_url, headers=headers, timeout=10)
        if user_resp.status_code != 200 or not user_resp.json().get('data'):
            return None
        user_data = user_resp.json()['data'][0]
        info['user'] = user_data
        user_id = user_data['id']

        stream_url = f'https://api.twitch.tv/helix/streams?user_id={user_id}'
        stream_resp = requests.get(stream_url, headers=headers, timeout=10)
        stream_data = (stream_resp.json()['data'][0]
                      if stream_resp.status_code == 200 and stream_resp.json().get('data') else {})
        info['stream'] = stream_data

        chan_url = f'https://api.twitch.tv/helix/channels?broadcaster_id={user_id}'
        chan_resp = requests.get(chan_url, headers=headers, timeout=10)
        chan_data = (chan_resp.json()['data'][0]
                     if chan_resp.status_code == 200 and chan_resp.json().get('data') else {})
        info['channel'] = chan_data

        tags_url = f'https://api.twitch.tv/helix/tags/streams?broadcaster_id={user_id}'
        tags_resp = requests.get(tags_url, headers=headers, timeout=10)
        tags = (tags_resp.json().get('data')
                if tags_resp.status_code == 200 and tags_resp.json().get('data') else [])
        info['tags'] = tags

        follows_url = f'https://api.twitch.tv/helix/users/follows?to_id={user_id}&first=1'
        follows_resp = requests.get(follows_url, headers=headers, timeout=10)
        follows_count = follows_resp.json().get('total', 0) if follows_resp.status_code == 200 else 0
        info['followers'] = follows_count
    except Exception as e:
        print(f"[ERROR][CHANNEL_INFO] Exception: {e}")
        return None

    return info

def batch_worker(stop_event, openai_client, assistant_id, model, thread_id, channel, client_id, token_manager, batch_interval=2):
    """Process queued messages in batches and moderate them."""
    batch = []
    last_send = time.time()
    channel_info = None
    last_channel_info_time = 0
    CHANNEL_INFO_REFRESH = 60

    while not stop_event.is_set() or not message_queue.empty():
        try:
            while not message_queue.empty() and len(batch) < 500:
                msg = message_queue.get()
                produced_ids.add(msg["id"])
                batch.append(msg)

            now = time.time()
            if now - last_channel_info_time > CHANNEL_INFO_REFRESH or channel_info is None:
                try:
                    channel_info = get_channel_info(channel, client_id, token_manager.get_token())
                    last_channel_info_time = now
                except Exception as e:
                    print(f"[WARN] Could not fetch channel info: {e}")
                    channel_info = None

            if batch and (now - last_send >= batch_interval):
                print(f"[INFO] Queuing batch of {len(batch)} messages for moderation...")
                run_queue.put((batch.copy(), channel_info))
                batch.clear()
                last_send = now
            time.sleep(0.05)
        except Exception as e:
            print(f"[ERROR][BATCH] {e}")

    if batch:
        print(f"[INFO] Final flush of {len(batch)} messages...")
        run_queue.put((batch.copy(), channel_info))
        batch.clear()

def run_worker(stop_event, openai_client, assistant_id, model, thread_id, client_id, token_manager):
    """Process batches from run_queue sequentially without blocking batch_worker."""
    while not stop_event.is_set() or not run_queue.empty():
        try:
            batch, channel_info = run_queue.get(timeout=1)
        except queue.Empty:
            continue

        ok = run_with_timeout(
            moderate_batch,
            args=(openai_client, assistant_id, model, thread_id, batch, channel_info, token_manager.get_token(), client_id),
            timeout=MODERATION_TIMEOUT_SECONDS,
        )

        if not ok:
            print(f"[ERROR][BATCH] Moderation failed or timed out or API did not respond. Marking {len(batch)} messages as NOT MODERATED and moving on.")
            debug_ids = [msg['id'] for msg in batch]
            not_moderated.update(debug_ids)
            print(f"[DEBUG][NOT-MODERATED] Message IDs not moderated (sample): {debug_ids[:10]}{' ...' if len(debug_ids) > 10 else ''}")

def loss_report():
    missing = produced_ids - consumed_ids
    print(f"\n[LOSS DETECTION]")
    print(f"  Total messages produced: {len(produced_ids)}")
    print(f"  Total messages consumed: {len(consumed_ids)}")
    print(f"  Total missing: {len(missing)}")
    if missing:
        print(f"  Missing message IDs: {list(missing)[:10]}{' ...' if len(missing) > 10 else ''}")
    if not_moderated:
        print(f"\n[NOT MODERATED]")
        print(f"  Total messages not moderated: {len(not_moderated)}")
        print(f"  Example IDs: {list(not_moderated)[:10]}{' ...' if len(not_moderated) > 10 else ''}")
