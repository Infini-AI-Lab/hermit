Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
Twelve 1 by 1 squares form a rectangle, as shown. What is the total area of the shaded region? [asy]
size(4cm);defaultpen(linewidth(0.75));

// Filled portions
fill((0, 4)--(0, 0)--(2, 0)--cycle, gray(0.75));
fill((0, 4)--(3, 4)--(3, 0)--cycle, gray(0.75));

// grid
int j;
for (j = 0; j < 4; ++j) {draw((j, 0)--(j, 4));}
for (j = 0; j < 5; ++j) {draw((0, j)--(3, j));}

//diagonals
draw((0, 4)--(3, 0)); draw((0, 4)--(2, 0));
[/asy]
