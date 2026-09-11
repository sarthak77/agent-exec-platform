from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChatRequest(_message.Message):
    __slots__ = ("messages", "approved")
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    APPROVED_FIELD_NUMBER: _ClassVar[int]
    messages: _containers.RepeatedCompositeFieldContainer[Message]
    approved: bool
    def __init__(self, messages: _Optional[_Iterable[_Union[Message, _Mapping]]] = ..., approved: _Optional[bool] = ...) -> None: ...

class Message(_message.Message):
    __slots__ = ("role", "content", "agent")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    role: str
    content: str
    agent: str
    def __init__(self, role: _Optional[str] = ..., content: _Optional[str] = ..., agent: _Optional[str] = ...) -> None: ...

class ChatResponse(_message.Message):
    __slots__ = ("messages", "token_usage", "finish_reason")
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    TOKEN_USAGE_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    messages: _containers.RepeatedCompositeFieldContainer[Message]
    token_usage: TokenUsage
    finish_reason: str
    def __init__(self, messages: _Optional[_Iterable[_Union[Message, _Mapping]]] = ..., token_usage: _Optional[_Union[TokenUsage, _Mapping]] = ..., finish_reason: _Optional[str] = ...) -> None: ...

class TokenUsage(_message.Message):
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")
    PROMPT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_TOKENS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    def __init__(self, prompt_tokens: _Optional[int] = ..., completion_tokens: _Optional[int] = ..., total_tokens: _Optional[int] = ...) -> None: ...
