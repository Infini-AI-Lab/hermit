Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
The graph of the equation $y = \frac{x}{x^3 + Ax^2 + Bx + C}$, where $A,B,C$ are integers, is shown below. Find $A + B + C$. [asy]
import graph; size(8.14cm); real lsf=0.5; pen dps=linewidth(0.7)+fontsize(10); defaultpen(dps); pen ds=black; real xmin=-3.52,xmax=4.62,ymin=-3.66,ymax=3.94;

pen cqcqcq=rgb(0.75,0.75,0.75);

/*grid*/ pen gs=linewidth(0.7)+cqcqcq+linetype("2 2"); real gx=1,gy=1;
for(real i=ceil(xmin/gx)*gx;i<=floor(xmax/gx)*gx;i+=gx) draw((i,ymin)--(i,ymax),gs); for(real i=ceil(ymin/gy)*gy;i<=floor(ymax/gy)*gy;i+=gy) draw((xmin,i)--(xmax,i),gs);

Label laxis; laxis.p=fontsize(10);

xaxis("$x$",xmin,xmax,Ticks(laxis,Step=1.0,Size=2,NoZero),Arrows(6),above=true); yaxis("$y$",ymin,ymax,Ticks(laxis,Step=1.0,Size=2,NoZero),Arrows(6),above=true); real f1(real x){return x/((x-1)*(x-2)*(x+2));} draw(graph(f1,-3.51,-2.01),linewidth(1.2)); draw(graph(f1,-1.99,0.99),linewidth(1.2)); draw(graph(f1,1.01,1.99),linewidth(1.2)); draw(graph(f1,2.01,4.61),linewidth(1.2));

clip((xmin,ymin)--(xmin,ymax)--(xmax,ymax)--(xmax,ymin)--cycle);

[/asy]
