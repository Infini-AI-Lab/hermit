Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
A square and a regular heptagon  are coplanar and share a common side $\overline{AD}$, as shown. What is the degree measure of angle $BAC$?  Express your answer as a common fraction.

[asy]
for(int i=0; i <=7; ++i) {
draw(dir(360*i/7+90)--dir(360*(i+1)/7+90));
}
pair A = dir(360*3/7+90);
pair F = dir(360*4/7+90);
pair C = A+dir(90)*(F-A);
pair D = C+F-A;
pair B = dir(360*2/7+90);

draw(A--C--D--F);

label("$A$",A,S);
label("$B$",B,W);
label("$C$",C,SE);
label("$D$",F,S);

[/asy]
