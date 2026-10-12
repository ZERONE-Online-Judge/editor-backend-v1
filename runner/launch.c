#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* Root-only launcher. Evidence is written through an FD closed in the child. */
int main(int argc, char **argv) {
    if (getuid() != 0 || argc < 7) return 125;
    uid_t uid = (uid_t)strtoul(argv[1], NULL, 10);
    if (uid != 1000 && uid != 1001) return 125;
    unsigned long cpu_ms = strtoul(argv[2], NULL, 10);
    unsigned long output_bytes = strtoul(argv[3], NULL, 10);
    int fd = open(argv[4], O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0 || prctl(PR_SET_CHILD_SUBREAPER, 1) != 0) return 125;
    pid_t child = fork();
    if (child < 0) return 125;
    if (child == 0) {
        close(fd);
        struct rlimit zero = {0, 0}, files = {128, 128};
        struct rlimit output = {output_bytes, output_bytes};
        unsigned long seconds = (cpu_ms + 999) / 1000;
        if (seconds < 1) seconds = 1;
        struct rlimit cpu = {seconds, seconds + 1};
        if (setrlimit(RLIMIT_CORE, &zero) || setrlimit(RLIMIT_NOFILE, &files) ||
            setrlimit(RLIMIT_FSIZE, &output) || setrlimit(RLIMIT_CPU, &cpu) ||
            setgroups(0, NULL) || setgid(uid) || setuid(uid) ||
            prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) _exit(125);
        execvp(argv[6], &argv[6]);
        perror("execvp");
        _exit(127);
    }
    double cpu_total = 0;
    long max_rss = 0;
    int main_status = 0, status;
    struct rusage usage;
    pid_t waited;
    /* Reap detached/orphan descendants as well, until the command tree ends. */
    while ((waited = wait4(-1, &status, 0, &usage)) > 0 || errno == EINTR) {
        if (waited < 0) continue;
        if (waited == child) main_status = status;
        cpu_total += usage.ru_utime.tv_sec * 1000.0 + usage.ru_utime.tv_usec / 1000.0;
        cpu_total += usage.ru_stime.tv_sec * 1000.0 + usage.ru_stime.tv_usec / 1000.0;
        if (usage.ru_maxrss > max_rss) max_rss = usage.ru_maxrss;
    }
    int code = WIFEXITED(main_status) ? WEXITSTATUS(main_status) : -WTERMSIG(main_status);
    dprintf(fd, "{\"cpu_ms\":%.3f,\"memory_kb\":%ld,\"exit_code\":%d}", cpu_total, max_rss, code);
    close(fd);
    return code < 0 ? 128 - code : code;
}
