Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
Two runners, $A$ and $B,$ start at a point $O$ on a linear track, and start running in the same direction.  Runner $B$ runs three times as fast as runner $A.$  An observer stands at point $P$ so that $\overline{OP}$ is perpendicular to the track.  Find the maximum of $\angle APB,$ in degrees.

[asy]
unitsize(2 cm);

pair A, B, O, P;

A = (0.4,0);
B = (1.2,0);
O = (0,0);
P = (0,1);

draw((-0.5,0)--(2,0));
draw(O--P);
draw(P--A);
draw(P--B);

label("$A$", A, S);
label("$B$", B, S);
label("$O$", O, S);
label("$P$", P, N);
[/asy]
