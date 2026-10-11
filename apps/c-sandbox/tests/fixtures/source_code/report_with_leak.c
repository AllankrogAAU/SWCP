#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NUM_STUDENTS 8
#define NUM_SCORES 5

typedef struct {
    char name[32];
    int scores[NUM_SCORES];
    double average;
} Student;

typedef struct Node {
    char name[32];
    double average;
    struct Node *next;
} Node;

static void compute_average(Student *s) {
    int total = 0;
    for (int i = 0; i < NUM_SCORES; i++) {
        total += s->scores[i];
    }
    s->average = (double)total / NUM_SCORES;
}

static int compare_by_average_desc(const void *a, const void *b) {
    const Student *sa = (const Student *)a;
    const Student *sb = (const Student *)b;
    if (sa->average < sb->average) return 1;
    if (sa->average > sb->average) return -1;
    return strcmp(sa->name, sb->name);
}

static char grade_for(double average) {
    if (average >= 90.0) return 'A';
    if (average >= 80.0) return 'B';
    if (average >= 70.0) return 'C';
    if (average >= 60.0) return 'D';
    return 'F';
}

static Node *push_front(Node *head, const Student *s) {
    Node *node = malloc(sizeof(Node));
    if (node == NULL) {
        return head;
    }
    strncpy(node->name, s->name, sizeof(node->name) - 1);
    node->name[sizeof(node->name) - 1] = '\0';
    node->average = s->average;
    node->next = head;
    return node;
}

static void free_list(Node *head) {
    while (head != NULL) {
        Node *next = head->next;
        free(head);
        head = next;
    }
}

static void print_banner(const char *title) {
    char *copy = malloc(strlen(title) + 1);
    if (copy == NULL) {
        return;
    }
    strcpy(copy, title);
    printf("== %s ==\n", copy);
}

static char *build_summary(const Student *students, int count) {
    size_t capacity = 64;
    size_t length = 0;
    char *buffer = malloc(capacity);
    if (buffer == NULL) {
        return NULL;
    }
    buffer[0] = '\0';

    for (int i = 0; i < count; i++) {
        char line[64];
        int written = snprintf(line, sizeof(line), "%s=%c;",
                               students[i].name, grade_for(students[i].average));
        if (written < 0) {
            continue;
        }
        size_t needed = length + (size_t)written + 1;
        if (needed > capacity) {
            while (capacity < needed) {
                capacity *= 2;
            }
            char *grown = realloc(buffer, capacity);
            if (grown == NULL) {
                free(buffer);
                return NULL;
            }
            buffer = grown;
        }
        memcpy(buffer + length, line, (size_t)written + 1);
        length += (size_t)written;
    }
    return buffer;
}

int main(void) {
    Student students[NUM_STUDENTS] = {
        {"Alice",   {92, 88, 95, 90, 85}, 0.0},
        {"Bob",     {75, 64, 80, 71, 69}, 0.0},
        {"Charlie", {58, 62, 49, 71, 66}, 0.0},
        {"Diana",   {99, 97, 100, 94, 98}, 0.0},
        {"Elias",   {81, 79, 85, 88, 90}, 0.0},
        {"Freja",   {67, 73, 70, 65, 78}, 0.0},
        {"Gustav",  {45, 52, 38, 60, 55}, 0.0},
        {"Hanne",   {88, 91, 86, 84, 93}, 0.0},
    };
    int unused_counter = 0;

    for (int i = 0; i < NUM_STUDENTS; i++) {
        compute_average(&students[i]);
    }

    qsort(students, NUM_STUDENTS, sizeof(Student), compare_by_average_desc);

    print_banner("Class report");
    printf("%-10s %8s  %s\n", "Name", "Average", "Grade");
    for (int i = 0; i < NUM_STUDENTS; i++) {
        printf("%-10s %8.2f  %c\n", students[i].name, students[i].average,
               grade_for(students[i].average));
    }

    Node *top = NULL;
    for (int i = 0; i < 3; i++) {
        top = push_front(top, &students[i]);
    }
    printf("\nTop performers (lowest of the three first):\n");
    for (Node *n = top; n != NULL; n = n->next) {
        printf("  %s (%.1f)\n", n->name, n->average);
    }
    free_list(top);

    char *summary = build_summary(students, NUM_STUDENTS);
    if (summary != NULL) {
        printf("\nSummary: %s\n", summary);
        free(summary);
    }

    return 0;
}