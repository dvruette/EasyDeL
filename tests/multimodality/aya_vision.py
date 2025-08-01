import jax
from jax import numpy as jnp
from transformers import AutoProcessor

import easydel as ed


def main():
    sharding_axis_dims = (1, 1, 1, -1, 1)

    prefill_length = 1024
    max_new_tokens = 1024

    max_length = max_new_tokens + prefill_length
    pretrained_model_name_or_path = "CohereForAI/aya-vision-8b"

    dtype = jnp.float16
    param_dtype = jnp.float16
    partition_axis = ed.PartitionAxis()

    processor = AutoProcessor.from_pretrained(pretrained_model_name_or_path)
    processor.padding_side = "left"

    model = ed.AutoEasyDeLModelForImageTextToText.from_pretrained(
        pretrained_model_name_or_path,
        auto_shard_model=True,
        sharding_axis_dims=sharding_axis_dims,
        config_kwargs=ed.EasyDeLBaseConfigDict(
            freq_max_position_embeddings=max_length,
            mask_max_position_embeddings=max_length,
            kv_cache_quantization_method=ed.EasyDeLQuantizationMethods.NONE,
            gradient_checkpointing=ed.EasyDeLGradientCheckPointers.NONE,
            attn_dtype=param_dtype,
            attn_mechanism=ed.AttentionMechanisms.VANILLA,
        ),
        quantization_method=ed.EasyDeLQuantizationMethods.A8BIT,
        param_dtype=param_dtype,
        dtype=dtype,
        partition_axis=partition_axis,
        precision=jax.lax.Precision.DEFAULT,
    )

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "image": "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/bee.jpg",
                },
                {"type": "text", "text": "Describe this image in detail."},
            ],
        },
    ]

    inputs = processor.apply_chat_template(
        messages,
        return_tensors="jax",
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
    )
    inference = ed.vInference(
        model=model,
        processor_class=processor,
        generation_config=ed.vInferenceConfig(
            max_new_tokens=max_new_tokens,
            sampling_params=ed.SamplingParams(
                max_tokens=max_new_tokens,
                temperature=0.8,
                top_p=0.95,
                top_k=10,
            ),
            eos_token_id=model.generation_config.eos_token_id,
            streaming_chunks=32,
            num_return_sequences=1,
        ),
    )
    inference.precompile(
        ed.vInferencePreCompileConfig(
            batch_size=1,
            prefill_length=prefill_length,
            vision_included=True,
            vision_batch_size=7,
            vision_channels=3,
            vision_height=364,
            vision_width=364,
        )
    )

    print("Start Generation Process.")
    for response in inference.generate(**inputs):  # noqa
        ...
    print(
        processor.batch_decode(
            response.sequences[..., response.padded_length :],
            skip_special_tokens=True,
        )[0]
    )
    print("\n" + ("*" * 5))
    print("TPS  :", response.tokens_per_second)


if __name__ == "__main__":
    main()
