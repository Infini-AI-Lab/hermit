Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
In quadrilateral $ABCD$, angle $BAD$ and angle $CDA$ are trisected as shown. What is the degree measure of angle $AFD$?

[asy]
size(150);
pair A , B, C, D; A = (0,0); B = (2, 4); C = (7,4); D = (7, -2);
draw( (0,0)--(2,4) -- (7,4) -- (7, -2)-- cycle);
label("$A$", A, SW);
label("$B$", B, NW);
label("$C$", C, NE);
label("$D$", D, SE);
pair E, F;
E = (4.5-.2,1-.2); F = (5, 3);
draw(A--E--D); draw(A--F--D);
label("$E$", E, N); label("$F$", F, NW);
dot(A);dot(B);dot(C);dot(D);dot(E);dot(F);
label("$x$", (1, 1.5), S); label("$x$", (2, 1), S+W); label("$x$", (2, -1), N+N+N+W);
label("$y$", (5.5+.3, .5-.3), S); label("$y$", (6.5+.3, 0)); label("$y$", (5+.5, -1.5+.3));
label("$110^{\circ}$",(2.5,3.5)); label("$100^{\circ}$",(6.5-.2,3.5));
[/asy]
