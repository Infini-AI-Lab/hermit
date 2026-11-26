Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
Below is a magic square, meaning that the sum of the numbers in each row, in each column, and in each of the $2$ main diagonals are equal. What is the value of $n$?

[asy]size(125);
for(int i = 0; i<4; ++i)
{

draw((0,i)--(3,i),linewidth(1));
}

for(int j = 0; j<4; ++j)
{

draw((j,0)--(j,3),linewidth(1));
}

label("$n-3$",(.5,.5));
label("3",(.5,1.5));
label("$n+1$",(.5,2.5));

label("$n+2$",(1.5,.5));
label("$2n-9$",(1.5,1.5));
label("$1$",(1.5,2.5));

label("$2$",(2.5,.5));
label("$n$",(2.5,1.5));
label("$n-1$",(2.5,2.5));
[/asy]
