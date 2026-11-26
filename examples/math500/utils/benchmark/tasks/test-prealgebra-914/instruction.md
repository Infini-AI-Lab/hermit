Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
In the diagram below, we have $\overline{ST}\parallel\overline{QR}$, $\angle P= 40^\circ$, and $\angle Q =35^\circ$. Find the measure of $\angle STR$ in degrees.

[asy]
pair P,Q,R,SS,T;
Q = (0,0);
R = (1,0);
P = (1.1,0.5);
SS = 0.6*P;
T = R + 0.6*(P-R);
draw(T--SS--P--R--Q--SS);
label("$P$",P,N);
label("$S$",SS,NW);
label("$Q$",Q,S);
label("$R$",R,S);
label("$T$",T,ENE);
[/asy]
