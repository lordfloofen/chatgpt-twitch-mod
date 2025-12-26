import time
import json
import logging
import queue
import requests
import re
import math
import threading
from utils import vprint, run_with_timeout

from token_utils import count_tokens, TokenBucket

# --- Message queue used for incoming messages from IRC ---
message_queue = queue.Queue()
run_queue = queue.Queue()

produced_ids = set()
consumed_ids = set()
not_moderated = set()

MAX_OPENAI_CONTENT_SIZE = 256000
MAX_RATE_LIMIT_RETRIES = 3


def configure_limits(
    max_openai_content_size: int | None = None,
    max_rate_limit_retries: int | None = None,
) -> None:
    """Override module limits from configuration."""
    global MAX_OPENAI_CONTENT_SIZE, MAX_RATE_LIMIT_RETRIES
    if max_openai_content_size is not None:
        try:
            MAX_OPENAI_CONTENT_SIZE = int(max_openai_content_size)
        except Exception:
            pass
    if max_rate_limit_retries is not None:
        try:
            MAX_RATE_LIMIT_RETRIES = int(max_rate_limit_retries)
        except Exception:
            pass


def _parse_retry_after_seconds(message: str, default: int = 5) -> int:
    """Extract retry delay from a rate limit error message."""
    if not message:
        return default
    match = re.search(r"try again in ([0-9.]+)s", message, re.IGNORECASE)
    if match:
        try:
            return max(default, math.ceil(float(match.group(1))))
        except Exception:
            return default
    return default


def delete_chat_message(broadcaster_id, moderator_id, message_id, token, client_id):
    url = "https://api.twitch.tv/helix/moderation/chat"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }
    params = {
        "broadcaster_id": broadcaster_id,
        "moderator_id": moderator_id,
        "message_id": message_id,
    }
    try:
        response = requests.delete(url, headers=headers, params=params, timeout=10)
        if response.status_code == 204:
            print(f"[TWITCH] Deleted message {message_id}")
            return True
        else:
            print(
                f"[TWITCH][ERROR] Failed to delete message {message_id}: {response.status_code} - {response.text}"
            )
            return False
    except Exception as e:
        print(f"[TWITCH][ERROR] Exception deleting message {message_id}: {e}")
        return False


def _extract_text_content(content):
    """Extract text value from a response content block."""
    if content is None:
        return None
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    if text is not None and hasattr(text, "value"):
        return getattr(text, "value", None)
    value = getattr(content, "value", None)
    if isinstance(value, str):
        return value
    return None


def _extract_response_text(response, streamed_chunks=None):
    """Collect text output from a Response object."""
    texts = []
    try:
        outputs = getattr(response, "output", None) or getattr(
            response, "outputs", None
        )
        for output in outputs or []:
            message = getattr(output, "message", None)
            contents = getattr(message, "content", None) or []
            for content in contents:
                value = _extract_text_content(content)
                if value:
                    texts.append(value)
    except Exception as e:
        print(f"[ERROR][RESPONSE][PARSE] {e}")
    if not texts:
        try:
            output_text = getattr(response, "output_text", None)
            if output_text:
                if isinstance(output_text, list):
                    texts.extend(str(chunk) for chunk in output_text)
                else:
                    texts.append(str(output_text))
        except Exception:
            pass
    if texts:
        return "".join(texts).strip()
    if streamed_chunks:
        return "".join(streamed_chunks).strip()
    return ""


def _request_assistant_response(
    openai_client, assistant_id, payload_text: str, use_stream: bool
):
    """Send a single-turn request to the assistant without threads."""
    streamed_chunks = []
    if use_stream:
        try:
            with openai_client.responses.stream(
                assistant_id=assistant_id,
                input=payload_text,
            ) as stream:
                for event in stream:
                    delta = getattr(event, "delta", None)
                    if delta:
                        text = _extract_text_content(delta)
                        if text:
                            streamed_chunks.append(text)
                    elif hasattr(event, "data"):
                        delta = getattr(event.data, "delta", None)
                        text = _extract_text_content(delta)
                        if text:
                            streamed_chunks.append(text)
                final_response = stream.get_final_response()
            return _extract_response_text(final_response, streamed_chunks)
        except Exception as e:
            print(f"[ERROR][MODERATION][STREAM] {e}")
            return None
    response = openai_client.responses.create(
        assistant_id=assistant_id,
        input=payload_text,
    )
    return _extract_response_text(response)


def moderate_batch(
    openai_client,
    assistant_id,
    batch,
    channel_info=None,
    token=None,
    client_id=None,
    token_bucket: TokenBucket = None,
    use_stream: bool = False,
):
    try:
        context = (
            {
                "game": (
                    channel_info.get("stream", {}).get("game_name")
                    if channel_info
                    else None
                ),
                "channel_info": channel_info,
            }
            if channel_info
            else {}
        )

        payload = {
            "context": context,
            "messages": batch,
        }
        batch_json = json.dumps(payload)
        if len(batch_json) > MAX_OPENAI_CONTENT_SIZE:
            print(
                f"[ERROR][MODERATION] Batch too large ({len(batch_json)} chars), splitting and retrying."
            )
            if len(batch) == 1:
                print(
                    f"[FATAL][MODERATION] Single message too large to send, skipping: {batch[0]['id']}"
                )
                return False
            mid = len(batch) // 2
            left = moderate_batch(
                openai_client,
                assistant_id,
                batch[:mid],
                channel_info,
                token,
                client_id,
                token_bucket,
                use_stream,
            )
            right = moderate_batch(
                openai_client,
                assistant_id,
                batch[mid:],
                channel_info,
                token,
                client_id,
                token_bucket,
                use_stream,
            )
            return left and right

        latest = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            if token_bucket is not None:
                tokens_needed = count_tokens(batch_json)
                token_bucket.consume(tokens_needed)
            try:
                latest = _request_assistant_response(
                    openai_client, assistant_id, batch_json, use_stream
                )
                if latest is None:
                    raise RuntimeError("Assistant response was empty.")
                break
            except Exception as e:
                msg = str(e)
                if "rate limit" in msg.lower():
                    wait = _parse_retry_after_seconds(msg)
                    print(
                        f"[RATE LIMIT] Response request hit rate limit, retrying in {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                print(f"[ERROR][MODERATION] Exception fetching response: {e}")
                return False
        else:
            print("[ERROR][MODERATION] Exceeded maximum retries due to rate limit")
            return False

        print(f"[MODERATION]\n{latest}\n{'='*40}")
        if latest:
            logging.info(f"[MODERATION RESULT] {latest}")
            try:
                flagged = json.loads(latest)
                if isinstance(flagged, list) and flagged:
                    broadcaster_id = channel_info["user"]["id"]
                    moderator_id = channel_info["user"]["id"]
                    for msg in flagged:
                        msg_id = msg.get("id")
                        if msg_id:
                            delete_chat_message(
                                broadcaster_id, moderator_id, msg_id, token, client_id
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
    user_login = channel.lstrip("#")
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }
    info = {}
    try:
        user_url = f"https://api.twitch.tv/helix/users?login={user_login}"
        user_resp = requests.get(user_url, headers=headers, timeout=10)
        if user_resp.status_code != 200 or not user_resp.json().get("data"):
            return None
        user_data = user_resp.json()["data"][0]
        info["user"] = user_data
        user_id = user_data["id"]

        stream_url = f"https://api.twitch.tv/helix/streams?user_id={user_id}"
        stream_resp = requests.get(stream_url, headers=headers, timeout=10)
        stream_data = (
            stream_resp.json()["data"][0]
            if stream_resp.status_code == 200 and stream_resp.json().get("data")
            else {}
        )
        info["stream"] = stream_data

        chan_url = f"https://api.twitch.tv/helix/channels?broadcaster_id={user_id}"
        chan_resp = requests.get(chan_url, headers=headers, timeout=10)
        chan_data = (
            chan_resp.json()["data"][0]
            if chan_resp.status_code == 200 and chan_resp.json().get("data")
            else {}
        )
        info["channel"] = chan_data

        tags_url = f"https://api.twitch.tv/helix/tags/streams?broadcaster_id={user_id}"
        tags_resp = requests.get(tags_url, headers=headers, timeout=10)
        tags = (
            tags_resp.json().get("data")
            if tags_resp.status_code == 200 and tags_resp.json().get("data")
            else []
        )
        info["tags"] = tags

        follows_url = (
            f"https://api.twitch.tv/helix/users/follows?to_id={user_id}&first=1"
        )
        follows_resp = requests.get(follows_url, headers=headers, timeout=10)
        follows_count = (
            follows_resp.json().get("total", 0)
            if follows_resp.status_code == 200
            else 0
        )
        info["followers"] = follows_count
    except Exception as e:
        print(f"[ERROR][CHANNEL_INFO] Exception: {e}")
        return None

    return info


def batch_worker(
    stop_event,
    openai_client,
    assistant_id,
    channel,
    client_id,
    token_manager,
    batch_interval=2,
):
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
            if (
                now - last_channel_info_time > CHANNEL_INFO_REFRESH
                or channel_info is None
            ):
                try:
                    channel_info = get_channel_info(
                        channel, client_id, token_manager.get_token()
                    )
                    last_channel_info_time = now
                except Exception as e:
                    print(f"[WARN] Could not fetch channel info: {e}")
                    channel_info = None

            if batch and (now - last_send >= batch_interval):
                vprint(
                    1,
                    f"[INFO] Queuing batch of {len(batch)} messages for moderation...",
                )
                run_queue.put((batch.copy(), channel_info))
                batch.clear()
                last_send = now
            time.sleep(0.05)
        except Exception as e:
            print(f"[ERROR][BATCH] {e}")

    if batch:
        vprint(1, f"[INFO] Final flush of {len(batch)} messages...")
        run_queue.put((batch.copy(), channel_info))
        batch.clear()


def run_worker(
    stop_event,
    openai_client,
    assistant_id,
    client_id,
    token_manager,
    token_bucket: TokenBucket,
    moderation_timeout=60,
    use_stream: bool = False,
):
    """Process batches from run_queue in parallel worker threads."""

    def _run_single_batch(batch, channel_info):
        try:
            ok = run_with_timeout(
                moderate_batch,
                args=(
                    openai_client,
                    assistant_id,
                    batch,
                    channel_info,
                    token_manager.get_token(),
                    client_id,
                    token_bucket,
                    use_stream,
                ),
                timeout=moderation_timeout,
            )
        except KeyboardInterrupt:
            stop_event.set()
            return
        except RuntimeError as e:
            print(f"[ERROR][MODERATION][WORKER] {e}")
            stop_event.set()
            return

        if not ok:
            print(
                f"[ERROR][BATCH] Moderation failed or timed out or API did not respond. Marking {len(batch)} messages as NOT MODERATED and moving on."
            )
            debug_ids = [msg["id"] for msg in batch]
            not_moderated.update(debug_ids)
            print(
                f"[DEBUG][NOT-MODERATED] Message IDs not moderated (sample): {debug_ids[:10]}{' ...' if len(debug_ids) > 10 else ''}"
            )

    worker_threads: list[threading.Thread] = []
    try:
        while not stop_event.is_set() or not run_queue.empty():
            try:
                batch, channel_info = run_queue.get(timeout=1)
            except queue.Empty:
                continue

            batch_thread = threading.Thread(
                target=_run_single_batch, args=(batch, channel_info), daemon=True
            )
            batch_thread.start()
            worker_threads.append(batch_thread)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        for t in worker_threads:
            t.join()


def loss_report():
    missing = produced_ids - consumed_ids
    print(f"\n[LOSS DETECTION]")
    print(f"  Total messages produced: {len(produced_ids)}")
    print(f"  Total messages consumed: {len(consumed_ids)}")
    print(f"  Total missing: {len(missing)}")
    if missing:
        print(
            f"  Missing message IDs: {list(missing)[:10]}{' ...' if len(missing) > 10 else ''}"
        )
    if not_moderated:
        print(f"\n[NOT MODERATED]")
        print(f"  Total messages not moderated: {len(not_moderated)}")
        print(
            f"  Example IDs: {list(not_moderated)[:10]}{' ...' if len(not_moderated) > 10 else ''}"
        )
