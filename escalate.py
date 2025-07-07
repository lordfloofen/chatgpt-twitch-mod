import json
import time
import queue
import requests
from utils import load_json, save_json

THREADS_FILE = "user_threads.json"

# Queue for escalation tasks
escalate_queue = queue.Queue()

# Global per-user thread mapping
user_threads = load_json(THREADS_FILE)


def load_user_threads():
    return load_json(THREADS_FILE)


def save_user_threads(data):
    save_json(THREADS_FILE, data)


def get_user_thread_id(openai_client, username, thread_map):
    user_key = username.lower()
    if user_key in thread_map:
        return thread_map[user_key]
    thread = openai_client.beta.threads.create()
    thread_map[user_key] = thread.id
    save_user_threads(thread_map)
    print(f"[THREAD][USER] Created new thread for {user_key}: {thread.id}")
    return thread.id


def wait_for_run_completion(openai_client, thread_id, run, poll_interval=5, timeout=120):
    start = time.time()
    while True:
        try:
            status = openai_client.beta.threads.runs.retrieve(run.id, thread_id=thread_id)
            if status.status == "completed":
                return status
            if status.status in ("failed", "cancelled", "expired"):
                print(f"[ERROR][ESCALATION] Run status for {run.id} is {status.status}")
                return status
            if time.time() - start > timeout:
                print(f"[ERROR][ESCALATION] Timed out waiting for run {run.id} on thread {thread_id}")
                return status
            time.sleep(poll_interval)
        except Exception as e:
            print(f"[ERROR][ESCALATION] Exception while waiting for run: {e}")
            return None


def escalate_user_action(openai_client, assistant_id, username, violations, thread_map, poll_interval=5, timeout=120):
    """Send a list of violations for a single user to the escalation assistant."""
    thread_id = get_user_thread_id(openai_client, username, thread_map)
    try:
        openai_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=json.dumps({"violations": violations}),
        )
        run = openai_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
        )
    except Exception as e:
        print(f"[ERROR][ESCALATION] Exception starting run: {e}")
        return None

    status = wait_for_run_completion(openai_client, thread_id, run, poll_interval, timeout)
    if not status or status.status != "completed":
        return None
    try:
        result = openai_client.beta.threads.messages.list(thread_id=thread_id)
        latest = result.data[0].content[0].text.value if result.data else ""
        return latest
    except Exception as e:
        print(f"[ERROR][ESCALATION] Exception fetching result: {e}")
        return None


def parse_escalation_result(text):
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[ERROR][ESCALATION] Failed to parse escalation result: {e}")
    return {}


def lookup_user_id(username, token, client_id):
    url = f"https://api.twitch.tv/helix/users?login={username}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200 and resp.json().get("data"):
            return resp.json()["data"][0]["id"]
        print(f"[TWITCH][ERROR] User lookup failed for {username}: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[TWITCH][ERROR] Exception looking up user {username}: {e}")
    return None


def timeout_user(
    broadcaster_id,
    moderator_id,
    user_id,
    token,
    client_id,
    duration,
    reason="Timeout by moderation bot",
):
    """Timeout a user for ``duration`` seconds using the Twitch Moderation API."""
    return ban_user(
        broadcaster_id,
        moderator_id,
        user_id,
        token,
        client_id,
        reason=reason,
        duration=duration,
    )


def ban_user(
    broadcaster_id,
    moderator_id,
    user_id,
    token,
    client_id,
    *,
    reason="Banned by moderation bot",
    duration=None,
):
    """Ban a user or put them in a timeout using Twitch's moderation API.

    This implements the `Ban User` endpoint described in the Twitch API docs:
    https://dev.twitch.tv/docs/api/reference/#ban-user

    If ``duration`` is provided, the user is timed out for that number of
    seconds. Otherwise, the user is banned indefinitely.
    """

    url = "https://api.twitch.tv/helix/moderation/bans"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    params = {
        "broadcaster_id": broadcaster_id,
        "moderator_id": moderator_id,
    }
    data = {"data": {"user_id": user_id}}
    if duration is not None:
        data["data"]["duration"] = int(duration)
    if reason:
        data["data"]["reason"] = reason

    try:
        resp = requests.post(url, headers=headers, params=params, json=data, timeout=10)
        if resp.status_code in (200, 201, 204):
            if duration is None:
                print(f"[TWITCH] Banned user {user_id}")
            else:
                print(f"[TWITCH] Timed out user {user_id} for {duration}s")
            return True
        print(f"[TWITCH][ERROR] Failed to ban {user_id}: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[TWITCH][ERROR] Exception banning {user_id}: {e}")
    return False


def warn_user(broadcaster_id, moderator_id, user_id, token, client_id, reason="Warning issued by moderation bot"):
    """Warn a user in chat using Twitch's moderation API."""
    url = "https://api.twitch.tv/helix/moderation/warnings"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    params = {
        "broadcaster_id": broadcaster_id,
        "moderator_id": moderator_id,
    }
    data = {"data": {"user_id": user_id, "reason": reason}}
    try:
        resp = requests.post(url, headers=headers, params=params, json=data, timeout=10)
        if resp.status_code in (200, 201, 204):
            print(f"[TWITCH] Warned user {user_id}")
            return True
        print(f"[TWITCH][ERROR] Failed to warn {user_id}: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[TWITCH][ERROR] Exception warning {user_id}: {e}")
    return False


def send_chat_message(broadcaster_id, moderator_id, token, client_id, message):
    """Send a chat message as the moderator."""
    url = "https://api.twitch.tv/helix/chat/messages"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    params = {
        "broadcaster_id": broadcaster_id,
        "moderator_id": moderator_id,
    }
    data = {"message": message}
    try:
        resp = requests.post(url, headers=headers, params=params, json=data, timeout=10)
        if resp.status_code in (200, 201, 204):
            print(f"[TWITCH] Sent chat message: {message}")
            return True
        print(f"[TWITCH][ERROR] Failed to send chat message: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[TWITCH][ERROR] Exception sending chat message: {e}")
    return False


def escalate_worker(stop_event, openai_client, assistant_id, token_manager, client_id):
    """Background worker to process escalation tasks without blocking moderation."""
    while not stop_event.is_set() or not escalate_queue.empty():
        try:
            task = escalate_queue.get(timeout=1)
        except queue.Empty:
            continue

        username = task.get("username")
        violations = task.get("violations") or []
        broadcaster_id = task.get("broadcaster_id")
        moderator_id = task.get("moderator_id")

        try:
            result_text = escalate_user_action(
                openai_client,
                assistant_id,
                username,
                violations,
                user_threads,
            )
            print(f"[ESCALATION] {result_text or ''}")
            result = parse_escalation_result(result_text)
            action = (result.get("action") or "").lower()
            notes = result.get("notes", "")
            if action == "warn" and username:
                user_id = lookup_user_id(username, token_manager.get_token(), client_id)
                if user_id:
                    warn_user(
                        broadcaster_id,
                        moderator_id,
                        user_id,
                        token_manager.get_token(),
                        client_id,
                        notes,
                    )
                send_chat_message(
                    broadcaster_id,
                    moderator_id,
                    token_manager.get_token(),
                    client_id,
                    f"@{username} {notes}"
                )
            elif action in {"timeout", "ban"} and username:
                user_id = lookup_user_id(username, token_manager.get_token(), client_id)
                if user_id:
                    if action == "timeout":
                        duration = int(result.get("length", result.get("duration", 600)))
                        timeout_user(
                            broadcaster_id,
                            moderator_id,
                            user_id,
                            token_manager.get_token(),
                            client_id,
                            duration,
                            notes,
                        )
                        send_chat_message(
                            broadcaster_id,
                            moderator_id,
                            token_manager.get_token(),
                            client_id,
                            f"@{username} {notes}"
                        )
                    else:
                        ban_user(
                            broadcaster_id,
                            moderator_id,
                            user_id,
                            token_manager.get_token(),
                            client_id,
                            reason=notes,
                        )
                        send_chat_message(
                            broadcaster_id,
                            moderator_id,
                            token_manager.get_token(),
                            client_id,
                            f"@{username} {notes}"
                        )
            save_user_threads(user_threads)
        except Exception as e:
            print(f"[ERROR][ESCALATE_WORKER] {e}")
        finally:
            escalate_queue.task_done()
