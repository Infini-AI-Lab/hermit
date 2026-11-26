Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
Triangle $ABC$ has its vertices $A$, $B$, and $C$ on the sides of a rectangle 4 units by 5 units as shown. What is the area of triangle $ABC$ in square units?

[asy]

fill((0,1)--(4,0)--(2,5)--cycle,lightgray);

for(int i=1; i < 5; ++i){
for(int k=1; k < 4; ++k){
draw((0,i)--(4,i),dashed);
draw((k,0)--(k,5),dashed);
} }

draw((0,0)--(4,0)--(4,5)--(0,5)--(0,0));

draw((0,1)--(4,0)--(2,5)--(0,1));

label("$A$",(0,1),W);
label("$B$",(4,0),SE);
label("$C$",(2,5),N);

[/asy]
