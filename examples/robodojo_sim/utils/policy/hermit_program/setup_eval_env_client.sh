#!/bin/bash
# The simulator's client, as RoboDojo's launcher starts it (the arguments and the call are those of XPolicyLab's
# policy/demo_policy/setup_eval_env_client.sh).
set -euo pipefail
bench_name=${1}; task_name=${2}; ckpt_name=${3}; env_cfg_type=${4}; action_type=${5}; seed=${6}
env_gpu_id=${7}; eval_env_conda_env=${8}; additional_info=${9}; policy_server_port=${10}
policy_server_ip=${11:-"localhost"}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
XPL_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BENCH_ROOT="$(cd "${XPL_ROOT}/.." && pwd)"
policy_name="$(basename "${SCRIPT_DIR}")"
bash "${XPL_ROOT}/utils/setup_env_client.sh" "${XPL_ROOT}/utils" "${XPL_ROOT}/policy/${policy_name}/deploy.yml" \
  "${eval_env_conda_env}" "${policy_server_port}" "${bench_name}" "${task_name}" "${env_cfg_type}" \
  "${policy_name}" "${additional_info}" "${BENCH_ROOT}" "${seed}" "${env_gpu_id}" "${policy_server_ip}"
