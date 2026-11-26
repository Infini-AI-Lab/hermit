Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
The number of games won by five softball teams are displayed in the graph. However, the names of the teams are missing. The following clues provide information about the teams:

1. The Tigers won more games than the Eagles.

2. The Patriots won more games than the Cubs, but fewer games than the Mounties.

3. The Cubs won more than 20 games.

How many games did the Patriots win? [asy]
size(150);
defaultpen(linewidth(0.7pt)+fontsize(8));

int i = 1;

draw((0,i*5)--(60,i*5)); ++i;

fill(shift(12,0)*((4,0)--(4,10)--(8,10)--(8,0)--cycle),purple);
draw(shift(12,0)*((4,0)--(4,10)^^(8,0)--(8,10)));
draw((0,i*5)--(60,i*5)); ++i;

fill((4,0)--(4,15)--(8,15)--(8,0)--cycle,purple);
draw((4,0)--(4,15));
draw((8,0)--(8,15));

for(i = 3; i <= 4; ++i)
{
draw((0,i*5)--(60,i*5));
}

fill(shift(24,0)*((4,0)--(4,25)--(8,25)--(8,0)--cycle),purple);
draw(shift(24,0)*((4,0)--(4,25)^^(8,0)--(8,25)));
draw((0,i*5)--(60,i*5)); ++i;

fill(shift(36,0)*((4,0)--(4,30)--(8,30)--(8,0)--cycle),purple);
draw(shift(36,0)*((4,0)--(4,30)^^(8,0)--(8,30)));
draw((0,i*5)--(60,i*5)); ++i;

fill(shift(48,0)*((4,0)--(4,35)--(8,35)--(8,0)--cycle),purple);
draw(shift(48,0)*((4,0)--(4,35)^^(8,0)--(8,35)));
draw((0,i*5)--(60,i*5)); ++i;
draw((0,i*5)--(60,i*5));

xaxis(Bottom,0,60,RightTicks(" ",N=5,n=1,Size=2));
yaxis(Left,0,40,LeftTicks(Step=5,Size=2));
yaxis(Right,0,40);

label("Softball Results",(30,46));
label(rotate(90)*"Number of Wins",(-10,20));
label("Teams",(30,-10));
for(i = 0; i < 5; ++i)
{
label("?",(6+12*i,-4));
}
[/asy]
