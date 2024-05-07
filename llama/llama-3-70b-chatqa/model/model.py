from threading import Thread
from typing import Dict

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    GenerationConfig,
    TextIteratorStreamer,
)

MODEL_NAME = "nvidia/Llama3-ChatQA-1.5-70B"
MAX_LENGTH = 512
TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 40
REPETITION_PENALTY = 1.0
NO_REPEAT_NGRAM_SIZE = 0
DO_SAMPLE = True
DEFAULT_STREAM = True

SYSTEM = "System: This is a chat between a user and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the user's questions based on the context. The assistant should also indicate when the answer cannot be found in the context."
INSTRUCTION = "Please give a full and complete answer for the question."


def get_formatted_input(messages, context):
    for item in messages:
        if item["role"] == "user":
            ## only apply this instruction for the first user turn
            item["content"] = INSTRUCTION + " " + item["content"]
            break

    conversation = (
        "\n\n".join(
            [
                "User: " + item["content"]
                if item["role"] == "user"
                else "Assistant: " + item["content"]
                for item in messages
            ]
        )
        + "\n\nAssistant:"
    )
    formatted_input = SYSTEM + "\n\n" + context + "\n\n" + conversation
    return formatted_input


class Model:
    def __init__(self, **kwargs):
        self.model = None
        self.tokenizer = None
        self._secrets = kwargs["secrets"]

    def load(self):
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME, device_map="auto", torch_dtype=torch.float16
        )

    def preprocess(self, request: dict):
        terminators = [
            self.tokenizer.eos_token_id,
            self.tokenizer.convert_tokens_to_ids("<|eot_id|>"),
        ]
        generate_args = {
            "max_length": request.get("max_tokens", MAX_LENGTH),
            "temperature": request.get("temperature", TEMPERATURE),
            "top_p": request.get("top_p", TOP_P),
            "top_k": request.get("top_k", TOP_K),
            "repetition_penalty": request.get("repetition_penalty", REPETITION_PENALTY),
            "no_repeat_ngram_size": request.get(
                "no_repeat_ngram_size", NO_REPEAT_NGRAM_SIZE
            ),
            "do_sample": request.get("do_sample", DO_SAMPLE),
            "use_cache": True,
            "eos_token_id": terminators,
            "pad_token_id": self.tokenizer.pad_token_id,
        }
        request["formatted_input"] = get_formatted_input(
            request.pop("messages"), request.pop("context")
        )
        request["generate_args"] = generate_args
        return request

    def stream(self, input_ids: list, generation_args: dict):
        streamer = TextIteratorStreamer(self.tokenizer)
        generation_config = GenerationConfig(**generation_args)
        generation_kwargs = {
            "input_ids": input_ids,
            "generation_config": generation_config,
            "return_dict_in_generate": True,
            "output_scores": True,
            "max_new_tokens": generation_args["max_length"],
            "streamer": streamer,
        }

        with torch.no_grad():
            # Begin generation in a separate thread
            thread = Thread(target=self.model.generate, kwargs=generation_kwargs)
            thread.start()

            # Yield generated text as it becomes available
            def inner():
                for text in streamer:
                    yield text
                thread.join()

        return inner()

    def predict(self, request: Dict):
        formatted_input = request.pop("formatted_input")
        stream = request.pop("stream", DEFAULT_STREAM)
        generation_args = request.pop("generate_args")

        inputs = self.tokenizer(formatted_input, return_tensors="pt")
        input_ids = inputs["input_ids"].to("cuda")

        if stream:
            return self.stream(input_ids, generation_args)

        with torch.no_grad():
            outputs = self.model.generate(input_ids=input_ids, **generation_args)
            output_text = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
            return {"output": output_text}
