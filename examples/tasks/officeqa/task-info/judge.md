# Scoring

The official OfficeQA scorer, strict (numeric tolerance 0): score 1 if the text inside the last
`<FINAL_ANSWER>` tag matches the reference answer (numbers compared after normalising formatting, the requested form
respected), otherwise 0. Feedback says correct or incorrect, what the agent answered, and the reference answer.
