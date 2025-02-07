# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Convert H3 checkpoints from the original repository.

URL: https://github.com/HazyResearch/H3/tree/main
"""


import argparse

import torch

from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer, H3Config, H3ForCausalLM


def get_h3_config(model_name):
    if model_name == "H3-125m":
        hidden_size = 768
        num_hidden_layers = 12
        num_attention_heads = 12
        attn_layer_idx = [6]

    elif model_name == "H3-355m":
        hidden_size = 1024
        num_hidden_layers = 24
        num_attention_heads = 16
        attn_layer_idx = [8, 16]

    elif model_name == "H3-1.3b":
        hidden_size = 2048
        num_hidden_layers = 24
        num_attention_heads = 16
        attn_layer_idx = [8, 16]

    elif model_name == "H3-2.7b":
        hidden_size = 2560
        num_hidden_layers = 32
        num_attention_heads = 20
        attn_layer_idx = [8, 16, 24]

    config = H3Config(
        hidden_size=hidden_size,
        num_hidden_layers=num_hidden_layers,
        num_attention_heads=num_attention_heads,
        attn_layer_idx=attn_layer_idx,
    )
    return config


def rename_key(name):
    if "backbone.embeddings" in name:
        name = name.replace("backbone", "h3")
    if "backbone.layers" in name:
        name = name.replace("backbone.layers", "h3.blocks")
    if "backbone.ln_f" in name:
        name = name.replace("backbone.ln_f", "h3.final_layernorm")

    return name


def convert_state_dict(orig_state_dict):
    for key in orig_state_dict.copy().keys():
        val = orig_state_dict.pop(key)
        orig_state_dict[rename_key(key)] = val

    return orig_state_dict


def convert_h3_checkpoint_to_pytorch(model_name, pytorch_dump_folder_path, push_to_hub=False):
    # Load original state dict
    model_name_to_repo_id = {
        "H3-125m": "danfu09/H3-125M",
        "H3-355m": "danfu09/H3-355M",
        "H3-1.3b": "danfu09/H3-1.3B",
        "H3-2.7b": "danfu09/H3-2.7B",
    }
    model_name_to_file_name = {
        "H3-125m": "model.pt",
        "H3-355m": "model.pt",
        "H3-1.3b": "model.pt",
        "H3-2.7b": "model-3attn.pt",
    }
    filepath = hf_hub_download(repo_id=model_name_to_repo_id[model_name], filename=model_name_to_file_name[model_name])
    state_dict = torch.load(filepath, map_location="cpu")
    if "pytorch-lightning_version" in state_dict:
        state_dict = {k[len("model.") :]: v for k, v in state_dict["state_dict"].items() if k.startswith("model.")}

    config = get_h3_config(model_name)

    # Update state dict
    if "backbone.ln_0.weight" in state_dict:
        n_layers = config.num_hidden_layers
        ln_weight = state_dict.pop(f"backbone.layers.{n_layers - 1}.norm2.weight")
        ln_bias = state_dict.pop(f"backbone.layers.{n_layers - 1}.norm2.bias")
        state_dict["backbone.ln_f.weight"] = ln_weight
        state_dict["backbone.ln_f.bias"] = ln_bias
        for l in reversed(range(n_layers)):
            ln_weight = state_dict.pop(f"backbone.layers.{l}.norm1.weight")
            ln_bias = state_dict.pop(f"backbone.layers.{l}.norm1.bias")
            state_dict[f"backbone.layers.{l}.norm2.weight"] = ln_weight
            state_dict[f"backbone.layers.{l}.norm2.bias"] = ln_bias
            if l > 0:
                ln_weight = state_dict.pop(f"backbone.layers.{l - 1}.norm2.weight")
                ln_bias = state_dict.pop(f"backbone.layers.{l - 1}.norm2.bias")
                state_dict[f"backbone.layers.{l}.norm1.weight"] = ln_weight
                state_dict[f"backbone.layers.{l}.norm1.bias"] = ln_bias
        ln_weight = state_dict.pop("backbone.ln_0.weight")
        ln_bias = state_dict.pop("backbone.ln_0.bias")
        state_dict["h3.blocks.0.norm1.weight"] = ln_weight
        state_dict["h3.blocks.0.norm1.bias"] = ln_bias

    state_dict = convert_state_dict(state_dict)

    # Load HF model, equip with weights
    model = H3ForCausalLM(config)
    model.load_state_dict(state_dict)
    model.eval()

    # verify logits
    device = "cuda" if torch.cuda.is_available() else "cpu"
    input_ids = torch.tensor([[101]], device=device)
    model.to(device)

    with torch.no_grad():
        outputs = model(input_ids)
        logits = outputs.logits

    print("Logits:", logits[0, :3, :3])
    if model_name == "H3-125m":
        expected_slice = torch.tensor([[5.9570, 7.0703, 4.4727]], device=device)
    elif model_name == "H3-355m":
        expected_slice = torch.tensor([[4.5926, 6.2018, 4.6021]], device=device)
    if model_name in ['H3-125m', 'H3-355m']:
        assert torch.allclose(logits[0, 0, :3], expected_slice, atol=1e-2)

    print("Generating text...")
    tokenizer = AutoTokenizer.from_pretrained("gpt2")
    inputs = tokenizer("I enjoy walking with my cute dog", return_tensors="pt").to(device)
    input_ids = inputs.input_ids
    max_length = input_ids.shape[1] + 128

    outputs = model.generate(input_ids=input_ids, max_length=max_length)

    print(tokenizer.decode(outputs[0], skip_special_tokens=True))

    if pytorch_dump_folder_path is not None:
        print(f"Saving PyTorch model to {pytorch_dump_folder_path}")
        model.save_pretrained(pytorch_dump_folder_path)

    if push_to_hub:
        print(f"Pushing {model_name} to the hub...")
        model.push_to_hub(f"nielsr/{model_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Required parameters
    parser.add_argument(
        "--model_name",
        default="H3-125m",
        type=str,
        choices=["H3-125m", "H3-355m", "H3-1.3b", "H3-2.7b"],
        help="Name of the H3 model you'd like to convert.",
    )
    parser.add_argument("--pytorch_dump_folder_path", default=None, type=str, help="Path to the output PyTorch model.")
    parser.add_argument(
        "--push_to_hub", action="store_true", help="Whether to push the model to the hub after converting."
    )
    args = parser.parse_args()
    convert_h3_checkpoint_to_pytorch(args.model_name, args.pytorch_dump_folder_path, args.push_to_hub)
