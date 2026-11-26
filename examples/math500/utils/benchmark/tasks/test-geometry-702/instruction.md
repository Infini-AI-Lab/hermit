Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
Five points $A$, $B$, $C$, $D$, and $O$ lie on a flat field.  $A$ is directly north of $O$, $B$ is directly west of $O$, $C$ is directly south of $O$, and $D$ is directly east of $O$. The  distance between $C$ and $D$ is 140 m.  A hot-air balloon is positioned in the air at $H$ directly above $O$. The balloon is held in place by four ropes $HA$, $HB$, $HC$, and $HD$.  Rope $HC$ has length 150 m and rope $HD$ has length 130 m. [asy]
size(250);
pair A, B, C, D, O, H, W, X, Y, Z;
O=(0,0);
A=(1,1);
D=(1.5,-.3);
B=(-1.5,.3);
C=(-1,-1);
H=(0,2.5);
W=(5/3)*(A+D);
X=(5/3)*(A+B);
Y=(-1)*(W);
Z=(-1)*(X);
draw(W--X--Y--Z--W);
draw(A--C);
draw(B--D);
draw(O--H, linewidth(1));
draw(A--H, dashed);
draw(B--H, dashed);
draw(C--H, dashed);
draw(D--H, dashed);
dot(A);
dot(B);
dot(C);
dot(D);
dot(O);
dot(H);
label("A", A, NE);
label("B", B, SW);
label("C", C, SE);
label("D", D, NE);
label("O", O, SE);
label("H", H, NW);
[/asy]

To reduce the total length of rope used, rope $HC$ and rope $HD$ are to be replaced by a single rope $HP$ where $P$ is a point on the straight line between $C$ and $D$. (The balloon remains at the same position $H$ above $O$ as described above.)  Determine the greatest length of rope that can be saved.
