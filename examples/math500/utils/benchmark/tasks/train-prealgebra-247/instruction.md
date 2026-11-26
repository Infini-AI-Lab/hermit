Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
Squares of side length 1 are arranged to form the figure shown. What is the perimeter of the figure? [asy]

size(6cm);

path sqtop = (0, 0)--(0, 1)--(1, 1)--(1, 0);

path sqright = (0, 1)--(1, 1)--(1, 0)--(0, 0);

path horiz = (0, 0)--(1, 0); path vert = (0, 0)--(0, 1);

picture pic;

draw(pic, shift(-4, -2) * unitsquare);

draw(pic, shift(-4, -1) * sqtop);

draw(pic, shift(-3, -1) * sqright);

draw(pic, shift(-2, -1) * sqright);

draw(pic, shift(-2, 0) * sqtop);

draw(pic, (-1, 1)--(0, 1)); draw(pic, (-1, 0)--(0, 0));

add(reflect((0, 0), (0, 1)) * pic); add(pic);

draw((0, 0)--(0, 1));

[/asy]
