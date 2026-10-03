export interface Assignment {
  id: number
  title: string
  description: string
  starterCode: string
}

export const assignments: Assignment[] = [
  {
    id: 1,
    title: 'Hello, World',
    description: 'Write a C program that prints "Hello, World!" to the screen.',
    starterCode: `#include <stdio.h>\n\nint main(void) {\n    return 0;\n}`,
  },
  {
    id: 2,
    title: 'Add two integers',
    description: 'Write a C program that adds two ints together and prints the result.',
    starterCode: `#include <stdio.h>\n\nint main(void) {\n    int a = 2;\n    int b = 3;\n    return 0;\n}`,
  },
  {
    id: 3,
    title: 'Loops',
    description: 'Use a for loop to print the numbers 1 to 10.',
    starterCode: `#include <stdio.h>\n\nint main(void) {\n    return 0;\n}`,
  },
]