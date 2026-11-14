# The program's interface

Your program is one Python file. RoboDojo-Sim runs the task; at every control step it gives your program the
observation and executes the actions your program returns.

```python
def reset():            # optional: called before every episode (a new layout of the same task), possibly
    ...                 # more than once before the episode's first step

def act(obs):           # required: the next actions, a list of 1 to 100 action dicts (a chunk)
    return [action, ...]
```

The file is loaded once per evaluation and again after an episode in which it failed, so module-level state can last
across episodes: put what belongs to one episode in `reset`. The program runs in a process with no network and no
access to the simulator or its files; numpy and the standard library are available. An exception in `act` or
`reset`, an answer later than 60 seconds, or an invalid chunk ends your program's control of that episode: the robot
holds its joints until the episode's horizon, and the episode fails.

## The robot

A bimanual ARX X5: two 6-joint arms, each with a one-degree-of-freedom gripper. The controller runs at 25 Hz: one
action is one step of 0.04 s. An episode ends when the task's success condition holds or at the task's horizon
(200 to 1900 steps, by task). Most tasks also require both arms to be back near their starting pose at the end.

## The observation

`obs` is a dict:

| Key | What it is |
|---|---|
| `obs["instruction"]` | the task's instruction for this episode, a string |
| `obs["vision"][cam]["color"]` | an RGB image, `uint8`, shape `(480, 640, 3)`, for `cam` in `cam_head`, `cam_left_wrist`, `cam_right_wrist` |
| `obs["state"]["left_arm_joint_state"]`, `["right_arm_joint_state"]` | the arm's 6 joint positions, radians |
| `obs["state"]["left_ee_pose"]`, `["right_ee_pose"]` | the end effector's pose, `[x, y, z, qw, qx, qy, qz]`, metres, in the environment's frame |
| `obs["state"]["left_ee_joint_state"]`, `["right_ee_joint_state"]` | the gripper, one number in 0..1 (the last commanded value) |
| `obs["additional_info"]["frequency"]` | 25 |

The dict has a few more entries that carry nothing about the scene (`env_idx`, `data_format_version`, an image's
`shape`, and `action`, the previous command).

Arrays are read-only numpy arrays. There is no depth image and no camera calibration in the observation, and no
object poses: what the scene holds is what the three cameras show.

## An action

A dict with the arm targets of **one** mode and, if you want to move them, the grippers:

| Mode | Keys | Values |
|---|---|---|
| joint | `left_arm_joint_state`, `right_arm_joint_state` | 6 absolute joint position targets each, radians |
| end effector | `left_ee_pose`, `right_ee_pose` | an absolute pose each, `[x, y, z, qw, qx, qy, qz]`, in the frame of the observation's poses; solved by inverse kinematics, and an unreachable pose is silently not executed |
| both | `left_ee_joint_state`, `right_ee_joint_state` | the gripper, one number in 0..1; which end is open is the robot's convention: find it from an observation after commanding it |

A key you leave out keeps its value from the observation you were given (the arm stays where it is, the gripper as
it was commanded). Values are lists of plain numbers (or numpy arrays), finite and within 1000 in magnitude. Joint
and end-effector keys must not be mixed in one action.
