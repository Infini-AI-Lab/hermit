Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
The double-bar graph shows the number of home runs hit by McGwire and Sosa during each month of the 1998 baseball season. At the end of which month were McGwire and Sosa tied in total number of home runs?

[asy]
draw((0,0)--(28,0)--(28,21)--(0,21)--(0,0)--cycle,linewidth(1));

for(int i = 1; i < 21; ++i)
{

draw((0,i)--(28,i));
}

for(int i = 0; i < 8; ++i)
{

draw((-1,3i)--(0,3i));
}

label("0",(-1,0),W);
label("3",(-1,3),W);
label("6",(-1,6),W);
label("9",(-1,9),W);
label("12",(-1,12),W);
label("15",(-1,15),W);
label("18",(-1,18),W);
label("21",(-1,21),W);

for(int i = 0; i < 8; ++i)
{

draw((4i,0)--(4i,-1));
}

filldraw((1,0)--(2,0)--(2,1)--(1,1)--(1,0)--cycle,gray,linewidth(1));
filldraw((5,0)--(6,0)--(6,10)--(5,10)--(5,0)--cycle,gray,linewidth(1));
filldraw((9,0)--(10,0)--(10,16)--(9,16)--(9,0)--cycle,gray,linewidth(1));
filldraw((13,0)--(14,0)--(14,10)--(13,10)--(13,0)--cycle,gray,linewidth(1));
filldraw((17,0)--(18,0)--(18,8)--(17,8)--(17,0)--cycle,gray,linewidth(1));
filldraw((21,0)--(22,0)--(22,10)--(21,10)--(21,0)--cycle,gray,linewidth(1));
filldraw((25,0)--(26,0)--(26,15)--(25,15)--(25,0)--cycle,gray,linewidth(1));

filldraw((6,0)--(7,0)--(7,6)--(6,6)--(6,0)--cycle,black,linewidth(1));
filldraw((10,0)--(11,0)--(11,7)--(10,7)--(10,0)--cycle,black,linewidth(1));
filldraw((14,0)--(15,0)--(15,20)--(14,20)--(14,0)--cycle,black,linewidth(1));
filldraw((18,0)--(19,0)--(19,9)--(18,9)--(18,0)--cycle,black,linewidth(1));
filldraw((22,0)--(23,0)--(23,13)--(22,13)--(22,0)--cycle,black,linewidth(1));
filldraw((26,0)--(27,0)--(27,11)--(26,11)--(26,0)--cycle,black,linewidth(1));

label("Mar",(2,0),S);
label("Apr",(6,0),S);
label("May",(10,0),S);
label("Jun",(14,0),S);
label("Jul",(18,0),S);
label("Aug",(22,0),S);
label("Sep",(26,0),S);
[/asy] [asy]
draw((30,6)--(40,6)--(40,15)--(30,15)--(30,6)--cycle,linewidth(1));
filldraw((31,7)--(34,7)--(34,10)--(31,10)--(31,7)--cycle,black,linewidth(1));
filldraw((31,11)--(34,11)--(34,14)--(31,14)--(31,11)--cycle,gray,linewidth(1));

label("McGwire",(36,12.5));
label("Sosa",(36,8.5));
[/asy]
