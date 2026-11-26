Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
In circle $J$, $HO$ and $HN$ are tangent to the circle at $O$ and $N$. Find the number of degrees in the sum of $m\angle J$ and $m\angle H$. [asy]
size(150);
import graph;
pair J = (0,0), H = (6,0), O, N;
path circ = Circle(J,3);
pair M = midpoint(J--H);
path secCirc = Circle(M,3);
pair[] tangentPoints = intersectionpoints(circ,secCirc);
O = tangentPoints[0]; N = tangentPoints[1];
draw(J--N--H--O--cycle);
draw(circ);
label("$H$",H,E);
label("$J$",J,W);
label("$N$",N,S);
label("$O$",O,NE);
[/asy]
