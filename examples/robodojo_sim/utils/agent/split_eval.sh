#!/usr/bin/env bash
# One evaluation with the simulator in RoboDojo's container (the image of its Dockerfile) and the policy server on
# the host, as RoboDojo's docker/README.md runs a policy: for a host on which Isaac Sim 5.1 does not install (it
# needs glibc 2.35). dojo.Engine runs it from the checkout in place of `scripts/robodojo.sh eval` when it is given
# an image (ROBODOJO_IMAGE); the environment it passes reaches the policy server, which starts the program's worker.
#
#   split_eval.sh IMAGE POLICY TASK CKPT POLICY_ENV EVAL_NUM ENV_CFG ACTION_TYPE SEED GPU RUN_ID CACHE
#
# The container sees the Assets (read-only), the checkout's eval_result/ and the installed policy (read-only: the
# episode loop that runs in the simulator's process); its caches go under CACHE, not the caller's home. It exits
# with docker's own status when the container could not be started (125 to 127), and 70 when the policy server
# did not come up or did not stay up: the engine reports those, rather than score them.
set -euo pipefail
image=$1; policy=$2; task=$3; ckpt=$4; policy_env=$5; eval_num=$6; env_cfg=$7; action_type=$8; seed=$9
gpu=${10}; run_id=${11}; cache=${12}
root="$(pwd -P)"
assets="$(cd Assets && pwd -P)"
container="hermit-robodojo-${run_id}"
owner="$(id -u):$(id -g)"
port="$(bash XPolicyLab/utils/get_free_port.sh)"
mkdir -p eval_result "$cache/warp" "$cache/ov-share" "$cache/ov-cache"

bash scripts/robodojo.sh server --policy-dir "XPolicyLab/policy/${policy}" --task "$task" --ckpt "$ckpt" \
  --policy-env "$policy_env" --policy-port "$port" --bind-host 127.0.0.1 --env-cfg "$env_cfg" \
  --action-type "$action_type" --seed "$seed" --policy-gpu "$gpu" &
server=$!
stop() {
  # The policy server and what it started: this script leads its process group when the engine runs it
  trap '' TERM INT
  kill -TERM -- "-$$" 2>/dev/null || kill -TERM "$server" 2>/dev/null || true
  docker kill "$container" >/dev/null 2>&1 || true
  # The container runs as root: what it left in eval_result/ and the caches is given back to the caller, who
  # removes it, whether the evaluation ended or was stopped
  docker run --rm --network none --entrypoint chown -v "$root/eval_result:/given/eval_result" \
    -v "$cache:/given/cache" "$image" -R "$owner" /given >/dev/null 2>&1 || true
}
trap stop EXIT
bash XPolicyLab/utils/wait_for_policy_server.sh 127.0.0.1 "$port" "$server" "Policy server" 600 || exit 70
kill -0 "$server" 2>/dev/null || { echo "split_eval: the policy server on port $port is not running" >&2; exit 70; }

# Inside the container. The image names one Vulkan driver file (VK_ICD_FILENAMES), and the NVIDIA runtime writes
# the host's under the host's own name (nvidia_icd.x86_64.json on RHEL): without the one that exists the renderer
# does not start and no camera delivers a frame.
inside='for icd in "${VK_ICD_FILENAMES:-}" /etc/vulkan/icd.d/nvidia_icd.json /etc/vulkan/icd.d/nvidia_icd.x86_64.json \
    /usr/share/vulkan/icd.d/nvidia_icd.json; do
  if [[ -f "$icd" ]]; then export VK_ICD_FILENAMES="$icd" VK_DRIVER_FILES="$icd"; break; fi
done
exec bash scripts/robodojo.sh client "$@"'
# RoboDojo's image renders only with every GPU shown to the container; the simulator's is --env-gpu
docker run --rm --name "$container" --gpus all --network host --ipc host -e ROBODOJO_RUN_ID="$run_id" \
  -v "$assets:/workspace/RoboDojo/Assets:ro" -v "$assets:$root/Assets:ro" \
  -v "$root/eval_result:/workspace/RoboDojo/eval_result" \
  -v "$root/XPolicyLab/policy/${policy}:/workspace/RoboDojo/XPolicyLab/policy/${policy}:ro" \
  -v "$cache/warp:/root/.cache/warp" -v "$cache/ov-share:/root/.local/share/ov" -v "$cache/ov-cache:/root/.cache/ov" \
  "$image" bash -c "$inside" client \
  --task "$task" --policy-dir "XPolicyLab/policy/${policy}" --policy-host 127.0.0.1 --policy-port "$port" \
  --ckpt "$ckpt" --eval-num "$eval_num" --seed "$seed" --env-cfg "$env_cfg" --action-type "$action_type" --env-gpu "$gpu"
