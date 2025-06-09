import ssl
import irc.client
from datetime import datetime, timezone
import hashlib

# If you want message_queue to be shared, import it from your moderation module or bot.py
# from moderation import message_queue
# For a clean module, message_queue is passed in at setup.

def run_irc_forever(config, token_manager, message_queue=None):
    """
    Connects to Twitch IRC and processes chat events indefinitely.
    Args:
        config (dict): Loaded config.yaml.
        token_manager (TwitchOAuthTokenManager): Token manager instance.
        message_queue (queue.Queue): Thread-safe queue to place messages for moderation.
    """
    server = config["twitch"]["server"]
    port = config["twitch"].get("port", 6697)
    nickname = config["twitch"]["nickname"]
    channel = config["twitch"]["channel"]

    class SSLFactory(irc.client.connection.Factory):
        def __init__(self):
            self.ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            self.ssl_context.check_hostname = True
            self.ssl_context.verify_mode = ssl.CERT_REQUIRED
            self._server_hostname = server
            self.bind_address = None

        def connect(self, address):
            sock = self.ssl_context.wrap_socket(
                irc.client.socket.socket(),
                server_hostname=address[0]
            )
            sock.connect(address)
            return sock

        @property
        def wrapper(self):
            def wrap(sock, *args, **kwargs):
                if 'server_hostname' not in kwargs or not kwargs['server_hostname']:
                    kwargs['server_hostname'] = self._server_hostname
                return self.ssl_context.wrap_socket(sock, *args, **kwargs)
            return wrap

    def on_connect(connection, event):
        # Request IRCv3 tags capability before joining channel
        connection.cap('REQ', ':twitch.tv/tags', ':twitch.tv/commands', ':twitch.tv/membership')
        connection.join(channel)
        print(f"[IRC] Connected and joined {channel}")

    def on_pubmsg(connection, event):
        """
        Handles incoming chat messages, parses tags, and queues for moderation.
        """
        user = event.source.nick
        message = event.arguments[0]
        timestamp = datetime.now(timezone.utc).isoformat()

        # Twitch sends tags as a list of dicts or a dict, normalize to dict
        tags_raw = getattr(event, 'tags', {}) or {}
        if isinstance(tags_raw, list):
            tags = {tag['key']: tag['value'] for tag in tags_raw}
        else:
            tags = tags_raw
        badges = tags.get('badges', '')

        msg_id = tags.get('id') or hashlib.md5((user + message + timestamp).encode()).hexdigest()
        msg_obj = {
            "id": msg_id,
            "user": user,
            "message": message,
            "timestamp": timestamp,
            "badges": badges,
            "tags": tags,
        }
        if message_queue:
            message_queue.put(msg_obj)
        #print(f"[IRC][{user}] {message}")

    while True:
        try:
            reactor = irc.client.Reactor()
            factory = SSLFactory()
            irc_token = f"oauth:{token_manager.get_token()}"
            c = reactor.server().connect(
                server, port, nickname, password=irc_token, connect_factory=factory
            )
            c.add_global_handler("welcome", on_connect)
            c.add_global_handler("pubmsg", on_pubmsg)
            print("[IRC] IRC connection established, event loop starting...")
            reactor.process_forever()
        except Exception as e:
            print(f"[IRC][WARN] Connection lost or error: {e}. Reconnecting in 5s...")
            import time
            time.sleep(5)
