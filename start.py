import os
import argparse
from typing import Optional

import torch
import transformers
from peft import AutoPeftModelForCausalLM
import logging
from transformers import AutoTokenizer
from huggingface_hub import login


def parse_arge():
    """Parse the arguments."""
    parser = argparse.ArgumentParser()
    # add model id and dataset path argument
    parser.add_argument(
        "--model_id",
        type=str,
        help="Model id to use for training.",
    )
    parser.add_argument(
        "--hf_token",
        type=str,
        help="huggingface token to access gated repositories",
    )
    parser.add_argument(
        "--tasks",
        type=str,
        help="eval tasks, separated by comma, example: hellaswag,mmlu",
    )
    parser.add_argument(
        "--is_lora",
        type=bool,
        default=False,
        help="is the model a LORA adapter model",
    )
    parser.add_argument(
        "--email",
        type=str,
        default="",
        help="Email address to receive the results in",
    )

    parser.add_argument(
        "--repository_id",
        type=str,
        default="",
        help="huggingface repository id to upload the merged model to",
    )

    args, _ = parser.parse_known_args()

    return args


if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    logger.setLevel(transformers.logging.INFO)
    transformers.logging.set_verbosity(transformers.logging.INFO)
    transformers.logging.enable_default_handler()
    transformers.logging.enable_explicit_format()

    # parse arguments
    script_args = parse_arge()
    if script_args.hf_token:
        print(f"Logging into the Hugging Face Hub with token {script_args.hf_token[:10]}...")
        login(token=script_args.hf_token)

    model_id = script_args.model_id
    # if is an s3 path, download the model to /tmp/model using s5cmd
    if model_id.startswith("s3://"):
        # add /* to model id but make sure it doesn't already have / at the end
        if model_id[-1] != "/":
            model_id += "/"
        os.system(f"s5cmd sync {model_id}* /tmp/model")
        model_id = "/tmp/model"
    if script_args.is_lora:
        # merge the model
        model = AutoPeftModelForCausalLM.from_pretrained(
            model_id,
            low_cpu_mem_usage=True,
            torch_dtype=torch.float16,
            use_auth_token=True,
        )
        model = model.merge_and_unload()
        merged_model_path = "/tmp/merged_model"
        model.save_pretrained(merged_model_path, safe_serialization=True, max_shard_size="10GB")
        # tokenizer = AutoTokenizer.from_pretrained(model_id)
        # tokenizer.save_pretrained(merged_model_path)
        if script_args.repository_id is not None and len(script_args.repository_id) > 0:
            print("uploading to hub")
            from huggingface_hub import HfApi

            api = HfApi()
            future = api.upload_folder(folder_path=merged_model_path, repo_id=script_args.repository_id,
                                       repo_type="model", run_as_future=True)
            future.add_done_callback(lambda p: print(f"Uploaded to {p.result()}"))
        model_id = merged_model_path

    model_args = {
        "pretrained": model_id,  # required: taken from UI, no default value
        "tensor_parallel_size": 8,
        "dtype": 'auto',
        "gpu_memory_utilization": 0.90,
        "trust_remote_code": True
    }

    model_args_str = ",".join([f"{k}={v}" for k, v in model_args.items()])
    cmd = f"lm_eval --model=vllm --model_args={model_args_str} --tasks={script_args.tasks} --batch_size=auto --output_path=/opt/ml/model/"
    print(f"Running command: {cmd}")
    code = os.system(cmd)
    if code != 0:
        raise Exception("lm_eval failed")
