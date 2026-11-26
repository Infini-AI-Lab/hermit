Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
A hexagon is inscribed in a circle: [asy]
pair pA, pB, pC, pD, pE, pF, pO;
pO = (0, 0);
pA = pO + dir(-10);
pB = pO + dir(60);
pC = pO + dir(130);
pD = pO + dir(170);
pE = pO + dir(-160);
pF = pO + dir(-80);
draw(pA--pB--pC--pD--pE--pF--pA);
label("$105^\circ$", pF, N * 2);
label("$110^\circ$", pB, SW * 1.5);
label("$\alpha$", pD, E);
draw(circle(pO, 1));
[/asy] What is the measure of $\alpha$, in degrees?
