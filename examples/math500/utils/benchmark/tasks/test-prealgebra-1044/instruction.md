Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
$\textbf{Juan's Old Stamping Grounds}$

Juan organizes the stamps in his collection by country and by the decade in which they were issued. The prices he paid for them at a stamp shop were: Brazil and France, $6$ cents each, Peru $4$ cents each, and Spain $5$ cents each. (Brazil and Peru are South American countries and France and Spain are in Europe.) [asy]
/* AMC8 2002 #8, 9, 10 Problem */
size(3inch, 1.5inch);
for ( int y = 0; y <= 5; ++y )
{

draw((0,y)--(18,y));
}
draw((0,0)--(0,5));
draw((6,0)--(6,5));
draw((9,0)--(9,5));
draw((12,0)--(12,5));
draw((15,0)--(15,5));
draw((18,0)--(18,5));

label(scale(0.8)*"50s", (7.5,4.5));
label(scale(0.8)*"4", (7.5,3.5));
label(scale(0.8)*"8", (7.5,2.5));
label(scale(0.8)*"6", (7.5,1.5));
label(scale(0.8)*"3", (7.5,0.5));

label(scale(0.8)*"60s", (10.5,4.5));
label(scale(0.8)*"7", (10.5,3.5));
label(scale(0.8)*"4", (10.5,2.5));
label(scale(0.8)*"4", (10.5,1.5));
label(scale(0.8)*"9", (10.5,0.5));

label(scale(0.8)*"70s", (13.5,4.5));
label(scale(0.8)*"12", (13.5,3.5));
label(scale(0.8)*"12", (13.5,2.5));
label(scale(0.8)*"6", (13.5,1.5));
label(scale(0.8)*"13", (13.5,0.5));

label(scale(0.8)*"80s", (16.5,4.5));
label(scale(0.8)*"8", (16.5,3.5));
label(scale(0.8)*"15", (16.5,2.5));
label(scale(0.8)*"10", (16.5,1.5));
label(scale(0.8)*"9", (16.5,0.5));

label(scale(0.8)*"Country", (3,4.5));
label(scale(0.8)*"Brazil", (3,3.5));
label(scale(0.8)*"France", (3,2.5));
label(scale(0.8)*"Peru", (3,1.5));
label(scale(0.8)*"Spain", (3,0.5));

label(scale(0.9)*"Juan's Stamp Collection", (9,0), S);
label(scale(0.9)*"Number of Stamps by Decade", (9,5), N);
[/asy] What was the average price, in cents, of his $70\text{'s}$ stamps? Round your answer to the nearest tenth of a cent.
