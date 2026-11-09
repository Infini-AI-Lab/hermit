"""OPRO's meta-prompt, the parsing of the optimizer's reply and the de-duplication, ported from google-deepmind/opro
(commit a76bdce2cbf6d4a0d1e570a6fcfe17be9c2abdd7; see NOTICE):

- `_bucketize_float`, `gen_ins_and_score_pairs_substr`: opro/optimization/opt_utils.py:47-87, verbatim (np.inf as
  math.inf);
- `gen_meta_prompt`: opt_utils.py:90-335, the branch of the GPT optimizers ("gpt-3.5-turbo", "gpt-4") with
  meta_prompt_type "both_instructions_and_exemplars", verbatim; the exemplars are given as (question, answer) pairs
  instead of the dataset and its indices (the loaders of MMLU, BBH and GSM8K at :208-218 are task-specific);
- `extract_instruction`: the <INS>...</INS> extraction of run_evolution, opt_utils.py:745-762;
- `polish_sentence`, `instruction_to_filename`: opro/evaluation/eval_utils.py:76-117 (the md5 of the UTF-8 text:
  upstream encodes ASCII, which raises on any other character).
"""
from __future__ import annotations

import hashlib
import math


def _bucketize_float(num, n_buckets=20):
    assert num >= 0 and num <= 1, "The given number must be between 0 and 1."
    return round(num * n_buckets)


def gen_ins_and_score_pairs_substr(
    old_instructions_and_scores,
    old_instruction_score_threshold=0.1,
    max_num_instructions=1000,
    return_str_only=False,
    num_score_buckets=math.inf,
):
    """Generate the string that includes instruction-score pairs."""
    assert num_score_buckets == math.inf or isinstance(num_score_buckets, int)
    old_instructions_and_scores_str = ""
    old_instructions_and_scores = sorted(
        old_instructions_and_scores, key=lambda x: x[1]
    )[-max_num_instructions:]
    old_instructions_and_scores_in_meta_prompt = []
    for instruction, score, i_step in old_instructions_and_scores:
        if (
            not old_instruction_score_threshold
            or score >= old_instruction_score_threshold
        ):
            old_instructions_and_scores_in_meta_prompt.append(
                (instruction, score, i_step)
            )
            if num_score_buckets == math.inf:
                score_to_show = round(score, 3)
            else:
                score_to_show = _bucketize_float(score, num_score_buckets)
            old_instructions_and_scores_str += (
                f"\ntext:\n{instruction}\nscore:\n{score_to_show}\n"
            )
    if return_str_only:
        return old_instructions_and_scores_str
    else:
        return (
            old_instructions_and_scores_str,
            old_instructions_and_scores_in_meta_prompt,
        )


def gen_meta_prompt(
    old_instructions_and_scores,
    instruction_pos,
    old_instruction_score_threshold=0.1,
    max_num_instructions=1000,
    few_shot_qa_pairs=False,
    include_qa=True,
    exemplars=(),
    instructions_before_exemplars=True,
    num_score_buckets=math.inf,
):
    """The meta-prompt of a GPT optimizer: the old instructions with their scores (ascending), then the exemplars
    (question, answer) when `few_shot_qa_pairs`, then the request for a new instruction."""
    assert instruction_pos in {"before_Q", "Q_begin", "Q_end", "A_begin"}
    assert num_score_buckets == math.inf or isinstance(num_score_buckets, int)

    meta_prompt = ""
    if instruction_pos == "A_begin":
        meta_prompt_old_instruction_part = (
            "Your task is to generate the answer starting sentence <Start>."
            " Below are some previous starting sentences with their scores."
            " The score ranges from 0 to 100.\n"
        )
    else:
        meta_prompt_old_instruction_part = (
            "Your task is to generate the instruction <INS>."
            " Below are some previous instructions with their scores."
            " The score ranges from 0 to 100.\n"
        )
    # add old instructions
    old_instructions_and_scores_str = gen_ins_and_score_pairs_substr(
        old_instructions_and_scores=old_instructions_and_scores,
        old_instruction_score_threshold=old_instruction_score_threshold,
        max_num_instructions=max_num_instructions,
        return_str_only=True,
        num_score_buckets=num_score_buckets,
    )
    meta_prompt_old_instruction_part += old_instructions_and_scores_str
    # add QA pairs if few_shot_qa_pairs == True
    meta_prompt_exemplar_part = ""
    if few_shot_qa_pairs:
        meta_prompt_exemplar_part += "Below are some problems.\n"
        for question, true_answer in exemplars:
            if include_qa:  # when "Q:" and "A:" are present in the prompt
                if instruction_pos == "before_Q":
                    meta_prompt_exemplar_part += f"\ninput:\n<INS>\nQ: {question}\nA:"
                elif instruction_pos == "Q_begin":
                    meta_prompt_exemplar_part += f"\ninput:\nQ: <INS>\n{question}\nA:"
                elif instruction_pos == "Q_end":
                    meta_prompt_exemplar_part += f"\ninput:\nQ: {question}\n<INS>\nA:"
                else:  # instruction_pos == "A_begin"
                    meta_prompt_exemplar_part += f"\nQ: {question}\nA: <Start>"
            else:  # when there're no "Q:" and "A:" in the prompt
                assert instruction_pos in {"Q_begin", "Q_end"}
                if instruction_pos == "Q_begin":
                    meta_prompt_exemplar_part += f"\nProblem:\n<INS>\n{question}\n"
                elif instruction_pos == "Q_end":
                    meta_prompt_exemplar_part += f"\nProblem:\n{question}\n<INS>\n"

            meta_prompt_exemplar_part += (
                f"\nGround truth answer:\n{true_answer}\n"
            )

    if few_shot_qa_pairs:
        if instructions_before_exemplars:
            meta_prompt += (
                meta_prompt_old_instruction_part
                + "\n\n"
                + meta_prompt_exemplar_part
            )
        else:
            meta_prompt += (
                meta_prompt_exemplar_part
                + "\n\n"
                + meta_prompt_old_instruction_part
            )
    else:
        meta_prompt += meta_prompt_old_instruction_part

    if instruction_pos == "A_begin":
        meta_prompt += (
            "\n\nGenerate a starting sentence that is different from all the"
            " <Start> sentences above, and has a higher score than all the"
            " <Start> sentences above. The starting sentence should begin with"
            " <Start> and end with </Start>. The starting sentence should be"
            " concise, effective, and generally applicable to all QA pairs"
            " above."
        )
    else:
        meta_prompt += (
            "\n\nGenerate an instruction that"
            " is different from all the instructions <INS> above,"
            " and has a higher score than all the instructions <INS> above."
            " The instruction should begin with <INS> and end with </INS>."
            " The instruction should be concise, effective,"
            " and generally applicable to all problems above."
        )
    return meta_prompt


def extract_instruction(raw_output, instruction_pos="Q_begin"):
    """The text between the start and end tags; from the beginning or to the end when a tag is missing."""
    if instruction_pos == "A_begin":
        start_string = "<Start>"
        end_string = "</Start>"
    else:
        start_string = "<INS>"
        end_string = "</INS>"
    if start_string not in raw_output:
        start_index = 0
    else:
        start_index = raw_output.index(start_string) + len(start_string)
    if end_string not in raw_output:
        end_index = len(raw_output)
    else:
        end_index = raw_output.index(end_string)
    return raw_output[start_index:end_index].strip()


def instruction_to_filename(instruction):
    """The md5 of an instruction: how run_evolution recognises one it has already evaluated."""
    m = hashlib.md5()
    m.update(instruction.encode("utf-8"))
    return m.hexdigest()


def polish_sentence(sentence, add_ending_punc=False):
    """Standardize the sentence to English syntax."""
    sentence = sentence.strip()
    if sentence:
        sentence = sentence.replace("**", "")
        if len(sentence) > 1:
            sentence = (
                sentence[0].upper() + sentence[1:]
            )  # capitalize the first letter
        if add_ending_punc and not (
            sentence.endswith(".")
            or sentence.endswith("?")
            or sentence.endswith("!")
        ):
            sentence += "."
    return sentence
