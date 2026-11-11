#!/bin/bash
# The policy server, as RoboDojo's launcher starts it (the arguments and the server command are those of
# XPolicyLab's policy/demo_policy/setup_eval_policy_server.sh). HERMIT_PROGRAM_WORKER, which the harness's engine
# exports, reaches model.py through the environment.
set -euo pipefail
bench_name=${1}; task_name=${2}; ckpt_name=${3}; env_cfg_type=${4}; action_type=${5}; seed=${6}
policy_gpu_id=${7}; policy_conda_env=${8}; policy_server_port=${9}; policy_server_host=${10:-"localhost"}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XPL_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
policy_name="$(basename "${SCRIPT_DIR}")"
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate "${policy_conda_env}"
exec env PYTHONWARNINGS=ignore::UserWarning CUDA_VISIBLE_DEVICES="${policy_gpu_id}" \
  python "${XPL_ROOT}/setup_policy_server.py" --config_path "${XPL_ROOT}/policy/${policy_name}/deploy.yml" \
    --overrides port="${policy_server_port}" host="${policy_server_host}" bench_name="${bench_name}" \
      task_name="${task_name}" ckpt_name="${ckpt_name}" env_cfg_type="${env_cfg_type}" seed="${seed}" \
      policy_name="${policy_name}" action_type="${action_type}"
