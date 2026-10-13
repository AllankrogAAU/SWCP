#include <stdio.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

int main(void) {
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr;
    addr.sin_family = AF_INET;
    addr.sin_port = htons(53);
    inet_pton(AF_INET, "8.8.8.8", &addr.sin_addr);
    int rc = connect(sock, (struct sockaddr *)&addr, sizeof(addr));
    printf("connect_rc=%d\n", rc);
    return 0;
}