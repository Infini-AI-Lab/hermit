#!/bin/bash
# XPolicyLab's standalone entry (policy/demo_policy/eval.sh): the policy server, then the simulator's client.
# RoboDojo's `scripts/robodojo.sh eval`, which the harness's engine runs, starts the two setup scripts itself and
# only requires that this file exists.
set -euo pipefail
bench_name=${1}; task_name=${2}; ckpt_name=${3}; env_cfg_type=${4}; action_type=${5}; seed=${6}
policy_gpu_id=${7}; env_gpu_id=${8}; policy_conda_env=${9}; eval_env_conda_env=${10}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XPL_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
port=$(bash "${XPL_ROOT}/utils/get_free_port.sh")
bash "${SCRIPT_DIR}/setup_eval_policy_server.sh" "${bench_name}" "${task_name}" "${ckpt_name}" "${env_cfg_type}" \
  "${action_type}" "${seed}" "${policy_gpu_id}" "${policy_conda_env}" "${port}" &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" 2>/dev/null || true' EXIT
bash "${XPL_ROOT}/utils/wait_for_policy_server.sh" localhost "${port}" "${SERVER_PID}" "Policy server" 1200
bash "${SCRIPT_DIR}/setup_eval_env_client.sh" "${bench_name}" "${task_name}" "${ckpt_name}" "${env_cfg_type}" \
  "${action_type}" "${seed}" "${env_gpu_id}" "${eval_env_conda_env}" \
  "ckpt_name=${ckpt_name},action_type=${action_type}" "${port}" localhost
