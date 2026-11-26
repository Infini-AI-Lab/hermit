Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
The wheel shown is spun twice, so that the numbers indicated by the pointer are randomly determined (with each number on the wheel being equally likely). The two numbers determined in this way are recorded. The first number is divided by 4, determining one of the remainders 1,2,3 marking the columns of the checkerboard shown. The second number is divided by 5, determining one of the remainders 1,2,3,4 marking the rows of the checkerboard. Finally, a checker is placed on the square where this column and row meet. What is the probability that the checker is placed on a shaded square of the checkerboard?

[asy]
unitsize(1cm);
draw(Circle((0,0),2),linewidth(0.7));
draw((1.7,1)--(-1.7,-1),linewidth(0.7));
draw((1.7,-1)--(-1.7,1),linewidth(0.7));
draw((0,2)--(0,-2));
label("1",(0.8,0.5),NW);
label("2",(0.8,-0.5),SW);
label("6",(-0.8,0.5),NE);
label("9",(-0.8,-0.5),SE);
label("3",(-0.7,0),W);
label("7",(0.7,0),E);
draw((-2.8,0)--(-2.1,0),Arrow);
label("Pointer",(-2.8,0),W);
fill((3,0)--(3,1)--(4,1)--(4,0)--cycle,gray(0.7));
fill((3,-2)--(3,-1)--(4,-1)--(4,-2)--cycle,gray(0.7));
fill((4,1)--(4,2)--(5,2)--(5,1)--cycle,gray(0.7));
fill((4,-1)--(4,0)--(5,0)--(5,-1)--cycle,gray(0.7));
fill((5,0)--(5,1)--(6,1)--(6,0)--cycle,gray(0.7));
fill((5,-2)--(5,-1)--(6,-1)--(6,-2)--cycle,gray(0.7));
draw((3,-2)--(3,2)--(6,2)--(6,-2)--cycle,linewidth(0.7));
draw((3,-1)--(6,-1),linewidth(0.7));
draw((3,0)--(6,0),linewidth(0.7));
draw((3,1)--(6,1),linewidth(0.7));
draw((4,-2)--(4,2),linewidth(0.7));
draw((5,-2)--(5,2),linewidth(0.7));
label("1",(3.5,-2),S);
label("2",(4.5,-2),S);
label("3",(5.5,-2),S);
label("1",(3,-1.5),W);
label("2",(3,-0.5),W);
label("3",(3,0.5),W);
label("4",(3,1.5),W);
[/asy]
