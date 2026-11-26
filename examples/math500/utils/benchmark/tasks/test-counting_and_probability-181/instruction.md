Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
There is a total of 70 squares of three sizes whose vertices are points on this rectangular $3\times n$ grid of points. What is the value of $n$?

[asy]

unitsize(0.4 inch);

dot((0,0),linewidth(9bp));
dot((1,0),linewidth(9bp));
dot((2,0),linewidth(9bp));
dot((0,1),linewidth(9bp));
dot((0,2),linewidth(9bp));
dot((1,1),linewidth(9bp));
dot((2,1),linewidth(9bp));
dot((1,2),linewidth(9bp));
dot((2,2),linewidth(9bp));

filldraw((2.95,-0.05)--(3.05,-0.05)--(3.05,0.05)--(2.95,0.05)--cycle,black);
filldraw((2.45,-0.05)--(2.55,-0.05)--(2.55,0.05)--(2.45,0.05)--cycle,black);
filldraw((3.45,-0.05)--(3.55,-0.05)--(3.55,0.05)--(3.45,0.05)--cycle,black);

filldraw((2.95,0.95)--(3.05,0.95)--(3.05,1.05)--(2.95,1.05)--cycle,black);
filldraw((2.45,0.95)--(2.55,0.95)--(2.55,1.05)--(2.45,1.05)--cycle,black);
filldraw((3.45,0.95)--(3.55,0.95)--(3.55,1.05)--(3.45,1.05)--cycle,black);

filldraw((2.95,1.95)--(3.05,1.95)--(3.05,2.05)--(2.95,2.05)--cycle,black);
filldraw((2.45,1.95)--(2.55,1.95)--(2.55,2.05)--(2.45,2.05)--cycle,black);
filldraw((3.45,1.95)--(3.55,1.95)--(3.55,2.05)--(3.45,2.05)--cycle,black);

dot((4,0),linewidth(9bp));
dot((5,0),linewidth(9bp));
dot((4,1),linewidth(9bp));
dot((5,1),linewidth(9bp));
dot((4,2),linewidth(9bp));
dot((5,2),linewidth(9bp));

[/asy]
