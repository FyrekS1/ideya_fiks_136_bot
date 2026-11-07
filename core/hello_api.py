import asyncio
from core.config import MODEL, build_client

PROMPT = "В двух предложениях объясни, для чего был создан проект"


async def hello(client, prompt=PROMPT):
    response = await client.chat.completions.create(
        reasoning_effort="none",  # только проверка доступности api, reasoning не нужен
        model=MODEL,
        max_tokens=8192,
        messages=[
            {
                "role": "system",
                "content": "Ты помогаешь учиться школьникам и работать учителям в проекте школа.синхрон",
            },
            {"role": "user", "content": prompt},
        ],
    )
    if not response.choices or response.choices[0].finish_reason != "stop":
        raise ValueError("Ответ не завершён")
    message = response.choices[0].message
    if message.refusal or not message.content:
        raise ValueError("Нет ответа модели")
    return message.content


if __name__ == "__main__":
    client = build_client()
    print(asyncio.run(hello(client)))
