"""Генерация ansible group_vars из config/stand.yaml (единый источник правды).

Запуск: python scripts/gen_group_vars.py [--stand config/stand.yaml]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

TEMPLATE = """\
---
# СГЕНЕРИРОВАНО из config/stand.yaml (scripts/gen_group_vars.py) — не править руками.
tts_repo_url: {repo_url}
tts_git_ref: {git_ref}
tts_model_id: {model_id}
tts_backend: {backend}
tts_transport: {transport}
tts_sample_rate: {sample_rate}

# backend: triton
tts_triton_runtime_repo: {triton_runtime_repo}
tts_triton_runtime_ref: {triton_runtime_ref}
tts_v3_runtime_ref: {v3_runtime_ref}
tts_v3_model_name: {v3_model_name}
tts_v3_grpc_port: {v3_grpc_port}
tts_triton_build_image: {triton_build_image}
tts_triton_base_image: {triton_base_image}
tts_triton_image: {triton_image}
tts_triton_http_port: {triton_http_port}
tts_triton_grpc_port: {triton_grpc_port}
tts_triton_metrics_port: {triton_metrics_port}
tts_bls_instance_num: {bls_instance_num}
tts_triton_max_batch_size: {triton_max_batch_size}
tts_hf_endpoint: {hf_endpoint}

# backend: triton_v3 — тюнинг латентности (см. config/stand.yaml)
tts_v3_token_hop_len: {v3_token_hop_len}
tts_v3_token2wav_instances: {v3_token2wav_instances}
tts_v3_vocoder_instances: {v3_vocoder_instances}
tts_v3_chunk_strategy: {v3_chunk_strategy}
tts_v3_bls_instance_num: {v3_bls_instance_num}
tts_v3_kv_cache_fraction: {v3_kv_cache_fraction}
tts_v3_llm_max_batch_size: {v3_llm_max_batch_size}

# backend: python_grpc
tts_port: {port}
tts_max_conc: {max_conc}
tts_stream: {stream}
tts_fp16: {fp16}
tts_load_trt: {load_trt}

gpu_image_optimized: {gpu_image_optimized}

prometheus_port: {prometheus_port}
prometheus_retention: {retention}
dcgm_exporter_port: {dcgm_port}
node_exporter_port: {node_port}

repo_dest: /opt/tts-loadtest
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stand", default="config/stand.yaml")
    p.add_argument("--out", default="ansible/inventory/group_vars/all.yml")
    args = p.parse_args()
    stand = yaml.safe_load(Path(args.stand).read_text(encoding="utf-8"))
    tts, mon, cloud = stand["tts"], stand["monitoring"], stand["cloud"]

    def flag(key: str, default: bool = True) -> str:
        return "true" if tts.get(key, default) else "false"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        TEMPLATE.format(
            repo_url=tts["repo_url"],
            git_ref=tts["git_ref"],
            model_id=tts["model_id"],
            backend=tts.get("backend", "triton"),
            transport=tts["transport"],
            sample_rate=tts["sample_rate"],
            triton_runtime_repo=tts.get("triton_runtime_repo", "https://github.com/FunAudioLLM/CosyVoice"),
            triton_runtime_ref=tts.get("triton_runtime_ref", "e8bf717"),
            v3_runtime_ref=tts.get("v3_runtime_ref", "HEAD"),
            v3_model_name=tts.get("v3_model_name", "cosyvoice3"),
            v3_grpc_port=tts.get("v3_grpc_port", 18001),
            triton_build_image="true" if tts.get("triton_build_image", True) else "false",
            triton_base_image=tts.get("triton_base_image", "nvcr.io/nvidia/tritonserver:25.12-trtllm-python-py3"),
            triton_image=tts.get("triton_image", "soar97/triton-cosyvoice:25.06"),
            triton_http_port=tts.get("triton_http_port", 8000),
            triton_grpc_port=tts.get("triton_grpc_port", 8001),
            triton_metrics_port=tts.get("triton_metrics_port", 8002),
            bls_instance_num=tts.get("bls_instance_num", 16),
            triton_max_batch_size=tts.get("triton_max_batch_size", 16),
            hf_endpoint=tts.get("hf_endpoint", "https://huggingface.co"),
            v3_token_hop_len=tts.get("v3_token_hop_len", 15),
            v3_token2wav_instances=tts.get("v3_token2wav_instances", 1),
            v3_vocoder_instances=tts.get("v3_vocoder_instances", 1),
            v3_chunk_strategy=tts.get("v3_chunk_strategy", "exponential"),
            v3_bls_instance_num=tts.get("v3_bls_instance_num", 10),
            v3_kv_cache_fraction=tts.get("v3_kv_cache_fraction", 0.4),
            v3_llm_max_batch_size=tts.get("v3_llm_max_batch_size", 64),
            port=tts["port"],
            max_conc=tts["max_conc"],
            stream=flag("stream"),
            fp16=flag("fp16"),
            load_trt=flag("load_trt"),
            gpu_image_optimized="true" if "gpu" in str(cloud.get("image_family", "")) else "false",
            prometheus_port=mon["prometheus_port"],
            retention=mon["retention"],
            dcgm_port=mon["dcgm_exporter_port"],
            node_port=mon["node_exporter_port"],
        ),
        encoding="utf-8",
    )
    print(f"group_vars: {out}")


if __name__ == "__main__":
    main()
