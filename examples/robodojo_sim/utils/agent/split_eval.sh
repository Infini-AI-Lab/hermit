#!/usr/bin/env bash
# One evaluation with the simulator in RoboDojo's container (the image of its Dockerfile) and the policy server on
# the host, as RoboDojo's docker/README.md runs a policy: for a host on which Isaac Sim 5.1 does not install (it
# needs glibc 2.35). dojo.Engine runs it from the checkout in place of `scripts/robodojo.sh eval` when it is given
# an image (ROBODOJO_IMAGE); the environment it passes reaches the policy server, which starts the program's worker.
#
#   split_eval.sh IMAGE POLICY TASK CKPT POLICY_ENV EVAL_NUM ENV_CFG ACTION_TYPE SEED GPU RUN_ID CACHE
#
# The container sees the Assets (read-only), the checkout's eval_result/ and the installed policy (read-only: the
# episode loop that runs in the simulator's process); its caches go under CACHE, not the caller's home.
set -euo pipefail
image=$1; policy=$2; task=$3; ckpt=$4; policy_env=$5; eval_num=$6; env_cfg=$7; action_type=$8; seed=$9
gpu=${10}; run_id=${11}; cache=${12}
root="$(pwd -P)"
assets="$(cd Assets && pwd -P)"
container="hermit-robodojo-${run_id}"
port="$(bash XPolicyLab/utils/get_free_port.sh)"

# The policy server in a process group of its own, so that all of it is stopped with this script
setsid bash scripts/robodojo.sh server --policy-dir "XPolicyLab/policy/${policy}" --task "$task" --ckpt "$ckpt" \
  --policy-env "$policy_env" --policy-port "$port" --bind-host 127.0.0.1 --env-cfg "$env_cfg" \
  --action-type "$action_type" --seed "$seed" --policy-gpu "$gpu" &
server=$!
stop() {
  docker kill "$container" >/dev/null 2>&1 || true
  kill -TERM -- "-$server" 2>/dev/null || kill -TERM "$server" 2>/dev/null || true
}
trap stop EXIT
bash XPolicyLab/utils/wait_for_policy_server.sh 127.0.0.1 "$port" "$server" "Policy server" 600

mkdir -p eval_result "$cache/warp" "$cache/ov-share" "$cache/ov-cache"
# Inside the container. The image names one Vulkan driver file (VK_ICD_FILENAMES), and the NVIDIA runtime writes
# the host's under the host's own name (nvidia_icd.x86_64.json on RHEL): without the one that exists the renderer
# does not start and no camera delivers a frame.
inside='for icd in "${VK_ICD_FILENAMES:-}" /etc/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.x86_64.json \
    /usr/share/vulkan/icd.d/nvidia_icd.json; do
  if [[ -f "$icd" ]]; then export VK_ICD_FILENAMES="$icd" VK_DRIVER_FILES="$icd"; break; fi
done
bash scripts/robodojo.sh client "$@"; rc=$?
chown -R "$OWNER" eval_result /root/.cache/warp /root/.local/share/ov /root/.cache/ov; exit $rc'
# The container runs as root: what it leaves in eval_result/ and the caches is given back to the caller
# (RoboDojo's image renders only with every GPU shown to the container; the simulator's is --env-gpu)
docker run --rm --name "$container" --gpus all --network host --ipc host \
  -e ROBODOJO_RUN_ID="$run_id" -e OWNER="$(id -u):$(id -g)" \
  -v "$assets:/workspace/RoboDojo/Assets:ro" -v "$assets:$root/Assets:ro" \
  -v "$root/eval_result:/workspace/RoboDojo/eval_result" \
  -v "$root/XPolicyLab/policy/${policy}:/workspace/RoboDojo/XPolicyLab/policy/${policy}:ro" \
  -v "$cache/warp:/root/.cache/warp" -v "$cache/ov-share:/root/.local/share/ov" -v "$cache/ov-cache:/root/.cache/ov" \
  "$image" bash -c "$inside" client \
  --task "$task" --policy-dir "XPolicyLab/policy/${policy}" --policy-host 127.0.0.1 --policy-port "$port" \
  --ckpt "$ckpt" --eval-num "$eval_num" --seed "$seed" --env-cfg "$env_cfg" --action-type "$action_type" --env-gpu "$gpu"
