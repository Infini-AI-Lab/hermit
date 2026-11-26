Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
The distances from a point $P$ to five of the vertices of a regular octahedron are 3, 7, 8, 9, and 11.  Find the distance from $P$ to the sixth vertex.

[asy]
import three;

size(125);
currentprojection = perspective(6,3,1);

triple A, B, C, D, E, F, P;

A = (1,0,0);
B = (-1,0,0);
C = (0,1,0);
D = (0,-1,0);
E = (0,0,1);
F = (0,0,-1);
P = (1.2,1.5,1);

draw(A--P,red);
draw(B--P,red);
draw(C--P,red);
draw(D--P,red);
draw(E--P,red);
draw(F--P,red);

draw(A--C);
draw(A--D);
draw(A--E);
draw(A--F);
draw(C--E--D--F--cycle);
draw(D--B--C,dashed);
draw(B--C,dashed);
draw(B--D,dashed);
draw(B--E,dashed);
draw(B--F,dashed);

label("$P$", P, NE);
[/asy]
