from anthropic import Anthropic
from anthropic.types import Message


class Claude:
    def __init__(self, model: str):
        self.client = Anthropic()
        self.model = model

    def add_user_message(self, messages: list, message):
        user_message = {
            "role": "user",
            "content": message.content
            if isinstance(message, Message)
            else message,
        }
        messages.append(user_message)

    def add_assistant_message(self, messages: list, message):
        assistant_message = {
            "role": "assistant",
            "content": message.content
            if isinstance(message, Message)
            else message,
        }
        messages.append(assistant_message)

    def text_from_message(self, message: Message):
        return "\n".join(
            [block.text for block in message.content if block.type == "text"]
        )

    def chat(
        self,
        messages,
        system=None,
        temperature=None,
        stop_sequences=[],
        tools=None,
        thinking=False,
    ) -> Message:
        params = {
            "model": self.model,
            "max_tokens": 8000,
            "messages": messages,
            "stop_sequences": stop_sequences,
        }

        # temperature só quando pedida explicitamente: os modelos atuais (Opus 4.7+,
        # Sonnet 5, Fable 5) rejeitam o parâmetro (400) — enviá-la sempre quebrava o
        # chat inteiro ao trocar o CLAUDE_MODEL para um modelo recente.
        if temperature is not None:
            params["temperature"] = temperature

        # forma atual (modelos 4.6+): adaptive. O antigo {"type": "enabled",
        # "budget_tokens": N} foi removido nos modelos atuais (400 se enviado).
        if thinking:
            params["thinking"] = {"type": "adaptive"}

        if tools:
            params["tools"] = tools

        if system:
            params["system"] = system

        message = self.client.messages.create(**params)
        return message
