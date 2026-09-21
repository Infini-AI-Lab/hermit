Each current-batch directory contains task.json, prediction.json,
trajectory.jsonl and reference_answer.json. Read all four for all five episodes
before writing feedback. Their complete contents must reach a later model request.
The controller releases these references only after sealing original predictions.
No future episodes, scores, source mappings or other-arm trajectories are supplied.
