Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
If two lines $l$ and $m$ have equations $y = -x + 6$, and $y = -4x + 6$, what is the probability that a point randomly selected in the 1st quadrant and below $l$ will fall between $l$ and $m$? Express your answer as a decimal to the nearest hundredth.

[asy]
import cse5; import olympiad;
size(150);
add(grid(8,8));
draw((0,0)--(8,0),linewidth(1.2));
draw((0,0)--(0,8),linewidth(1.2));
label("$x$",(8,0),E);
label("$y$",(0,8),N);
draw((6,0)--(0,6)--(3/2,0));
label("$l$",(6,0)--(0,6),NE);
label("$m$",(0,6)--(3/2,0),NE);
[/asy]
