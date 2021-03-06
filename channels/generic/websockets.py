from django.core.serializers.json import json, DjangoJSONEncoder

from ..channel import Group, Channel
from ..auth import channel_session_user_from_http
from ..sessions import enforce_ordering
from .base import BaseConsumer


class WebsocketConsumer(BaseConsumer):
    """
    Base WebSocket consumer. Provides a general encapsulation for the
    WebSocket handling model that other applications can build on.
    """

    # You shouldn't need to override this
    method_mapping = {
        "websocket.connect": "raw_connect",
        "websocket.receive": "raw_receive",
        "websocket.disconnect": "raw_disconnect",
    }

    # Turning this on passes the user over from the HTTP session on connect,
    # implies channel_session_user
    http_user = False

    # Set one to True if you want the class to enforce ordering for you
    slight_ordering = False
    strict_ordering = False

    def get_handler(self, message, **kwargs):
        """
        Pulls out the path onto an instance variable, and optionally
        adds the ordering decorator.
        """
        # HTTP user implies channel session user
        if self.http_user:
            self.channel_session_user = True
        # Get super-handler
        self.path = message['path']
        handler = super(WebsocketConsumer, self).get_handler(message, **kwargs)
        # Optionally apply HTTP transfer
        if self.http_user:
            handler = channel_session_user_from_http(handler)
        # Ordering decorators
        if self.strict_ordering:
            return enforce_ordering(handler, slight=False)
        elif self.slight_ordering:
            return enforce_ordering(handler, slight=True)
        else:
            return handler

    def connection_groups(self, **kwargs):
        """
        Group(s) to make people join when they connect and leave when they
        disconnect. Make sure to return a list/tuple, not a string!
        """
        return []

    def raw_connect(self, message, **kwargs):
        """
        Called when a WebSocket connection is opened. Base level so you don't
        need to call super() all the time.
        """
        for group in self.connection_groups(**kwargs):
            Group(group, channel_layer=message.channel_layer).add(message.reply_channel)
        self.connect(message, **kwargs)

    def connect(self, message, **kwargs):
        """
        Called when a WebSocket connection is opened.
        """
        pass

    def raw_receive(self, message, **kwargs):
        """
        Called when a WebSocket frame is received. Decodes it and passes it
        to receive().
        """
        if "text" in message:
            self.receive(text=message['text'], **kwargs)
        else:
            self.receive(bytes=message['bytes'], **kwargs)

    def receive(self, text=None, bytes=None, **kwargs):
        """
        Called with a decoded WebSocket frame.
        """
        pass

    def send(self, text=None, bytes=None, close=False):
        """
        Sends a reply back down the WebSocket
        """
        message = {}
        if close:
            message["close"] = True
        if text is not None:
            message["text"] = text
        elif bytes is not None:
            message["bytes"] = bytes
        else:
            raise ValueError("You must pass text or bytes")
        self.message.reply_channel.send(message)

    @classmethod
    def group_send(cls, name, text=None, bytes=None, close=False):
        message = {}
        if close:
            message["close"] = True
        if text is not None:
            message["text"] = text
        elif bytes is not None:
            message["bytes"] = bytes
        else:
            raise ValueError("You must pass text or bytes")
        Group(name).send(message)

    def close(self):
        """
        Closes the WebSocket from the server end
        """
        self.message.reply_channel.send({"close": True})

    def raw_disconnect(self, message, **kwargs):
        """
        Called when a WebSocket connection is closed. Base level so you don't
        need to call super() all the time.
        """
        for group in self.connection_groups(**kwargs):
            Group(group, channel_layer=message.channel_layer).discard(message.reply_channel)
        self.disconnect(message, **kwargs)

    def disconnect(self, message, **kwargs):
        """
        Called when a WebSocket connection is opened.
        """
        pass


class JsonWebsocketConsumer(WebsocketConsumer):
    """
    Variant of WebsocketConsumer that automatically JSON-encodes and decodes
    messages as they come in and go out. Expects everything to be text; will
    error on binary data.
    """

    def raw_receive(self, message, **kwargs):
        if "text" in message:
            self.receive(json.loads(message['text']), **kwargs)
        else:
            raise ValueError("No text section for incoming WebSocket frame!")

    def receive(self, content, **kwargs):
        """
        Called with decoded JSON content.
        """
        pass

    def send(self, content, close=False):
        """
        Encode the given content as JSON and send it to the client.
        """
        super(JsonWebsocketConsumer, self).send(text=json.dumps(content), close=close)

    @classmethod
    def group_send(cls, name, content, close=False):
        WebsocketConsumer.group_send(name, json.dumps(content), close=close)


class WebsocketDemultiplexer(JsonWebsocketConsumer):
    """
    JSON-understanding WebSocket consumer subclass that handles demultiplexing
    streams using a "stream" key in a top-level dict and the actual payload
    in a sub-dict called "payload". This lets you run multiple streams over
    a single WebSocket connection in a standardised way.

    Incoming messages on streams are mapped into a custom channel so you can
    just tie in consumers the normal way. The reply_channels are kept so
    sessions/auth continue to work. Payloads must be a dict at the top level,
    so they fulfill the Channels message spec.

    Set a mapping from streams to channels in the "mapping" key. We make you
    whitelist channels like this to allow different namespaces and for security
    reasons (imagine if someone could inject straight into websocket.receive).
    """

    mapping = {}

    def receive(self, content, **kwargs):
        # Check the frame looks good
        if isinstance(content, dict) and "stream" in content and "payload" in content:
            # Match it to a channel
            stream = content['stream']
            if stream in self.mapping:
                # Extract payload and add in reply_channel
                payload = content['payload']
                if not isinstance(payload, dict):
                    raise ValueError("Multiplexed frame payload is not a dict")
                payload['reply_channel'] = self.message['reply_channel']
                # Send it onto the new channel
                Channel(self.mapping[stream]).send(payload)
            else:
                raise ValueError("Invalid multiplexed frame received (stream not mapped)")
        else:
            raise ValueError("Invalid multiplexed frame received (no channel/payload key)")

    def send(self, stream, payload):
        self.message.reply_channel.send(self.encode(stream, payload))

    @classmethod
    def group_send(cls, name, stream, payload, close=False):
        message = cls.encode(stream, payload)
        if close:
            message["close"] = True
        Group(name).send(message)

    @classmethod
    def encode(cls, stream, payload):
        """
        Encodes stream + payload for outbound sending.
        """
        return {"text": json.dumps({
            "stream": stream,
            "payload": payload,
        }, cls=DjangoJSONEncoder)}
