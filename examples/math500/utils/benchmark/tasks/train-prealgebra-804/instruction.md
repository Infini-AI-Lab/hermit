Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
In the diagram, square $ABCD$ has sides of length $4,$ and $\triangle ABE$ is equilateral. Line segments $BE$ and $AC$ intersect at $P.$ Point $Q$ is on $BC$ so that $PQ$ is perpendicular  to $BC$ and $PQ=x.$ [asy]
pair A, B, C, D, E, P, Q;
A=(0,0);
B=(4,0);
C=(4,-4);
D=(0,-4);
E=(2,-3.464);
P=(2.535,-2.535);
Q=(4,-2.535);
draw(A--B--C--D--A--E--B);
draw(A--C);
draw(P--Q, dashed);
label("A", A, NW);
label("B", B, NE);
label("C", C, SE);
label("D", D, SW);
label("E", E, S);
label("P", P, W);
label("Q", Q, dir(0));
label("$x$", (P+Q)/2, N);
label("4", (A+B)/2, N);
[/asy] Determine the measure of angle $BPC.$
