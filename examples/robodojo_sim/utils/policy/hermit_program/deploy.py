"""The episode loop RoboDojo imports into the simulator's process (XPolicyLab's contract: `eval_one_episode`). It is
the harness's and fixed: this process holds the task's privileged state (the success check, the objects' poses),
and nothing of it goes to the policy but the observation RoboDojo's `get_obs` returns. The loop is XPolicyLab's
demo policy's: reset, then observation, action chunk, the chunk's actions with an observation between them."""


def eval_one_episode(TASK_ENV, model_client):
    model_client.call(func_name="reset")
    while not TASK_ENV.is_episode_end():
        model_client.call(func_name="update_obs", obs=TASK_ENV.get_obs())
        actions = model_client.call(func_name="get_action")
        for index, action in enumerate(actions):
            TASK_ENV.take_action(action)
            if TASK_ENV.is_episode_end() or index + 1 == len(actions):
                break
            model_client.call(func_name="update_obs", obs=TASK_ENV.get_obs())
