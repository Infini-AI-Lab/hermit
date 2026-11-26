Solve the following math problem efficiently and clearly.  The last line of your response should be of the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' (without quotes) where ANSWER is just the final number or expression that solves the problem. Think step by step before answering.
What is the ratio of the area of the shaded square to the area of the large square? (The figure is drawn to scale.) [asy]
/* AMC8 1998 #13P */
size(1inch,1inch);
pair r1c1=(0,0), r1c2=(10,0), r1c3=(20,0), r1c4=(30, 0), r1c5=(40, 0);
pair r2c1=(0,10), r2c2=(10,10), r2c3=(20,10), r2c4=(30, 10), r2c5=(40, 10);
pair r3c1=(0,20), r3c2=(10,20), r3c3=(20,20), r3c4=(30, 20), r3c5=(40, 20);
pair r4c1=(0,30), r4c2=(10,30), r4c3=(20,30), r4c4=(30, 30), r4c5=(40, 30);
pair r5c1=(0,40), r5c2=(10,40), r5c3=(20,40), r5c4=(30, 40), r5c5=(40, 40);
draw(r1c1--r5c1--r5c5--r1c5--r1c1--r5c5);
draw(r5c1--r3c3);
draw(r4c4--r2c4--r3c5);
fill(r2c2--r3c3--r2c4--r1c3--cycle);
[/asy]
