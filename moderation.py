import time
import json
import logging
import queue
import requests
import re
import math
import threading
from utils import vprint, run_with_timeout

from token_utils import count_tokens, TokenBucket, PROMPT_TEXT

# --- Message queue used for incoming messages from IRC ---
message_queue = queue.Queue()
run_queue = queue.Queue()

_ids_lock = threading.Lock()
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
    """Collect text output from a Responses API Response object."""
    text = getattr(response, "output_text", None)
    if not text:
        texts = []
        try:
            for output in getattr(response, "output", None) or []:
                if getattr(output, "type", None) != "message":
                    continue
                for content in getattr(output, "content", None) or []:
                    value = _extract_text_content(content)
                    if value:
                        texts.append(value)
        except Exception as e:
            print(f"[ERROR][RESPONSE][PARSE] {e}")
        text = "".join(texts)
    if text:
        return text.strip()
    if streamed_chunks:
        return "".join(streamed_chunks).strip()
    return ""


def _request_moderation_response(
    openai_client, model, payload_text: str, use_stream: bool
):
    """Send a single-turn moderation request to the OpenAI Responses API."""
    if use_stream:
        streamed_chunks = []
        try:
            with openai_client.responses.stream(
                model=model,
                instructions=PROMPT_TEXT,
                input=payload_text,
                store=False,
            ) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "response.output_text.delta":
                        streamed_chunks.append(event.delta)
                final_response = stream.get_final_response()
            return _extract_response_text(final_response, streamed_chunks)
        except Exception as e:
            print(f"[ERROR][MODERATION][STREAM] {e}")
            return None
    response = openai_client.responses.create(
        model=model,
        instructions=PROMPT_TEXT,
        input=payload_text,
        store=False,
    )
    return _extract_response_text(response)


def moderate_batch(
    openai_client,
    model,
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
                model,
                batch[:mid],
                channel_info,
                token,
                client_id,
                token_bucket,
                use_stream,
            )
            right = moderate_batch(
                openai_client,
                model,
                batch[mid:],
                channel_info,
                token,
                client_id,
                token_bucket,
                use_stream,
            )
            return left and right

        latest = None
        if token_bucket is not None:
            tokens_needed = count_tokens(batch_json)
            token_bucket.consume(tokens_needed)
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                latest = _request_moderation_response(
                    openai_client, model, batch_json, use_stream
                )
                if latest is None:
                    raise RuntimeError("Moderation response was empty.")
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
                    if not channel_info or "user" not in channel_info:
                        print(
                            f"[ERROR][MODERATION] channel_info unavailable, cannot delete {len(flagged)} flagged message(s)"
                        )
                    elif not channel_info.get("moderator"):
                        print(
                            f"[ERROR][MODERATION] moderator identity unavailable, cannot delete {len(flagged)} flagged message(s)"
                        )
                    else:
                        broadcaster_id = channel_info["user"]["id"]
                        moderator_id = channel_info["moderator"]["id"]
                        for msg in flagged:
                            msg_id = msg.get("id")
                            if msg_id:
                                delete_chat_message(
                                    broadcaster_id, moderator_id, msg_id, token, client_id
                                )
            except Exception as e:
                print(f"[ERROR][MODERATION][DELETE] Failed to parse/delete: {e}")

        with _ids_lock:
            for msg in batch:
                consumed_ids.add(msg["id"])
        return True

    except Exception as e:
        print(f"[ERROR][MODERATION][UNHANDLED] {e}")
        return False


def get_moderator_info(client_id, token):
    """Look up the Twitch user identity that owns the current access token.

    Twitch's moderation endpoints require ``moderator_id`` to match the user
    ID embedded in the OAuth token, which is not necessarily the broadcaster
    (the bot commonly runs as a separate moderator account).
    """
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }
    try:
        resp = requests.get(
            "https://api.twitch.tv/helix/users", headers=headers, timeout=10
        )
        if resp.status_code == 200 and resp.json().get("data"):
            return resp.json()["data"][0]
    except Exception as e:
        print(f"[ERROR][MODERATOR_INFO] Exception: {e}")
    return None


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
        info["moderator"] = get_moderator_info(client_id, token)

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

        # Fetch channel tags (non-critical, endpoint may be deprecated)
        try:
            chan_tags = chan_data.get("tags", []) if chan_data else []
            info["tags"] = chan_tags if chan_tags else []
        except Exception:
            info["tags"] = []

        # Fetch follower count via the current endpoint
        try:
            follows_url = f"https://api.twitch.tv/helix/channels/followers?broadcaster_id={user_id}&first=1"
            follows_resp = requests.get(follows_url, headers=headers, timeout=10)
            follows_count = (
                follows_resp.json().get("total", 0)
                if follows_resp.status_code == 200
                else 0
            )
            info["followers"] = follows_count
        except Exception:
            info["followers"] = 0
    except Exception as e:
        print(f"[ERROR][CHANNEL_INFO] Exception: {e}")
        return None

    return info


def batch_worker(
    stop_event,
    openai_client,
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
                with _ids_lock:
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
    model,
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
                    model,
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
            with _ids_lock:
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
    with _ids_lock:
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
