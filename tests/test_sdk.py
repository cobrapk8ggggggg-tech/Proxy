from openai import OpenAI


def test_official_openai_sdk_chat_stream_and_responses(env):
    c, app, p, account, h = env
    sdk = OpenAI(
        api_key=h["Authorization"][7:],
        base_url="http://testserver/v1",
        http_client=c,
        max_retries=0,
    )
    result = sdk.chat.completions.create(
        model="gpt", messages=[{"role": "user", "content": "SDK hello"}]
    )
    assert result.choices[0].message.content and result.object == "chat.completion"
    chunks = list(
        sdk.chat.completions.create(
            model="gpt",
            messages=[{"role": "user", "content": "SDK stream"}],
            stream=True,
            stream_options={"include_usage": True},
        )
    )
    assert len({c.id for c in chunks}) == 1
    response = sdk.responses.create(model="gpt", input="SDK responses")
    assert response.output_text
    events = list(
        sdk.responses.create(
            model="gpt", input="SDK response stream", stream=True, store=False
        )
    )
    assert events[-1].type == "response.completed"
