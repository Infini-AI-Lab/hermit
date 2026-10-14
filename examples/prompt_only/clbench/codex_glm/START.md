You are improving a harness adapter: the files an acting agent receives (context, tools, skills) when it works on a
task. Your job in this call is to change the adapter so the acting agent does better on tasks like the ones in the
learning material, within the adapter's specification.

{message}
How to work:
1. Read the task description ({task_info}/README.md) and the adapter's guide ({adapter}/EVOLVER.md): it says which
   files you may change and in what format.
2. Read the learning material of this step ({learning_material}/current/): each task's input, the agent's output, the
   judge's score and feedback, and the full trajectory. Earlier steps are next to it; index.jsonl shows the trend.
3. Decide what to change, and change only files in {adapter}. Keep what still helps; generalise from the material
   instead of writing down facts about individual tasks.
4. Call the `check_spec` tool before you finish; fix what it reports.
5. Write {output}/report.md: first line one sentence on what you changed and why, then the details.
You may keep notes for later calls in {state}. Do not touch anything else.
