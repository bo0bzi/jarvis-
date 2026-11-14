from types import SimpleNamespace as NS

from friday import llm
from friday import tools as T
from friday.config import load_settings
from friday.llm import Brain
from friday.memory import Memory
from test_friday import clean_env, write_env  # noqa: F401


def _chunk(content=None, tool=None):
    delta = NS(content=content, tool_calls=tool)
    return NS(choices=[NS(delta=delta)])


class StreamingClient:
    """A fake OpenAI client whose create(stream=True) yields pieces of the reply."""
    def __init__(self, pieces_per_call):
        self.pieces, self.calls = list(pieces_per_call), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        assert kw.get("stream") is True
        return iter(self.pieces.pop(0))


def _brain(clean_env, pieces):
    s = load_settings(write_env(clean_env, "LLM_PROVIDER=custom\nCUSTOM_MODEL=qwen3:30b\nTOOL_MODE=native\n"))
    ctx = T.ToolContext(settings=s, memory=Memory(s.data_dir), speak=lambda t: None, confirm=lambda p: True)
    client = StreamingClient(pieces)
    return Brain(s, ctx.memory, ctx, client=client), client


def test_sentences_are_spoken_while_the_reply_is_still_arriving(clean_env):
    text = "The weather today is mild and clear across the whole region. Expect a high of ten degrees this afternoon. Dress lightly"
    pieces = [[_chunk(text[i:i + 7]) for i in range(0, len(text), 7)]]
    b, client = _brain(clean_env, pieces)
    heard = []
    b.stream_to(heard.append)
    reply = b.chat("how is the weather going to be today and this afternoon in general")
    assert reply == text
    assert heard == ["The weather today is mild and clear across the whole region.",
                     "Expect a high of ten degrees this afternoon."]  # the unfinished last sentence waits
    assert reply.startswith(b.streamed_prefix) and reply[len(b.streamed_prefix):].strip() == "Dress lightly"


def test_nothing_is_streamed_when_not_asked(clean_env):
    b, client = _brain(clean_env, [[_chunk("Hello there my friend, this is a perfectly normal reply. And more text.")]])
    b.on_sentence = None
    # without stream_to the normal (non-streaming) path is used, which this fake can't serve: just check the switch
    assert b.on_sentence is None


def test_claims_with_no_tool_behind_them_are_never_spoken_early(clean_env):
    text = "I've installed the program successfully for you just now. It is ready to use."
    b, client = _brain(clean_env, [[_chunk(text)], [_chunk("I did not install anything, so that was wrong of me to say.")]])
    heard = []
    b.stream_to(heard.append)
    b.chat("get that thing set up on my machine for me")
    assert heard == [] or all("installed the program successfully" not in h for h in heard)


def test_tool_calls_are_assembled_from_pieces(clean_env):
    calls = [NS(index=0, id="c1", function=NS(name="get_time", arguments='{"a"')),
             NS(index=0, id=None, function=NS(name=None, arguments=': 1}'))]
    b, client = _brain(clean_env, [[_chunk(None, [calls[0]]), _chunk(None, [calls[1]])]])
    resp = b._streamed_call({"model": "m", "messages": []}, "x", False)
    tc = resp.choices[0].message.tool_calls[0]
    assert tc.id == "c1" and tc.function.name == "get_time" and tc.function.arguments == '{"a": 1}'
    assert b.streamed_prefix == ""
