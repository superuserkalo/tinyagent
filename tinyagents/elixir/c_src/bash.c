#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define OUTPUT_LIMIT 131072

extern char **environ;
static volatile sig_atomic_t stopping;

/* The live supervisor owns job-control cleanup; the port helper owns its PID. */
static const char supervisor[] =
    "exec 3>&2 2>/dev/null\n"
    "set -m\n"
    "job=\n"
    "cancelled=\n"
    "stop() {\n"
    "    cancelled=1\n"
    "    if [ -n \"$job\" ]; then kill -KILL -- \"-$job\" 2>/dev/null; fi\n"
    "}\n"
    "trap stop TERM INT\n"
    "bash -c \"$1\" </dev/null 2>&3 3>&- &\n"
    "job=$!\n"
    "if [ -n \"$cancelled\" ]; then stop; fi\n"
    "wait \"$job\"\n"
    "result=$?\n"
    "kill -KILL -- \"-$job\" 2>/dev/null\n"
    "if [ -n \"$cancelled\" ]; then wait \"$job\" 2>/dev/null; exit 130; fi\n"
    "exit \"$result\"\n";

static void stop(int number) {
    (void)number;
    stopping = 1;
}

static int write_all(const void *bytes, size_t length) {
    const unsigned char *cursor = bytes;
    while (length) {
        ssize_t count = write(STDOUT_FILENO, cursor, length);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return -1;
        cursor += count;
        length -= (size_t)count;
    }
    return 0;
}

/* Erlang packet: four-byte length, then stdout=1, stderr=2, status=3, error=4. */
static int emit(unsigned char tag, const void *bytes, size_t length) {
    uint32_t header = htonl((uint32_t)length + 1);
    return write_all(&header, sizeof(header)) || write_all(&tag, 1) ||
           write_all(bytes, length);
}

static int make_pipe(int descriptors[2]) {
    if (pipe(descriptors) < 0) return errno;
    for (int i = 0; i < 2; ++i) {
        if (fcntl(descriptors[i], F_SETFD, FD_CLOEXEC) < 0) return errno;
    }
    if (fcntl(descriptors[0], F_SETFL, O_NONBLOCK) < 0) return errno;
    return 0;
}

static int start(pid_t *child, char *command, int output[2], int errors[2]) {
    posix_spawn_file_actions_t files;
    posix_spawnattr_t attributes;
    int error = posix_spawn_file_actions_init(&files);
    if (error) return error;
    error = posix_spawnattr_init(&attributes);
    if (error) {
        posix_spawn_file_actions_destroy(&files);
        return error;
    }
    error = posix_spawn_file_actions_addopen(&files, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
    if (!error) error = posix_spawn_file_actions_adddup2(&files, output[1], STDOUT_FILENO);
    if (!error) error = posix_spawn_file_actions_adddup2(&files, errors[1], STDERR_FILENO);
    int descriptors[] = {output[0], output[1], errors[0], errors[1]};
    for (int i = 0; i < 4 && !error; ++i)
        error = posix_spawn_file_actions_addclose(&files, descriptors[i]);
    sigset_t empty, defaults;
    sigemptyset(&empty);
    sigemptyset(&defaults);
    int signals[] = {SIGINT, SIGTERM, SIGQUIT, SIGHUP, SIGPIPE, SIGCHLD};
    for (int i = 0; i < 6; ++i) sigaddset(&defaults, signals[i]);
    if (!error) error = posix_spawnattr_setsigmask(&attributes, &empty);
    if (!error) error = posix_spawnattr_setsigdefault(&attributes, &defaults);
    if (!error)
        error = posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    char *arguments[] = {"bash", "--noprofile", "--norc", "-c", (char *)supervisor,
                         "tinyagent-bash", command, NULL};
    if (!error) error = posix_spawnp(child, "bash", &files, &attributes, arguments, environ);
    posix_spawnattr_destroy(&attributes);
    posix_spawn_file_actions_destroy(&files);
    return error;
}

static int capture(pid_t child, int descriptors[2], unsigned char bytes[2][OUTPUT_LIMIT],
                   size_t lengths[2], int *ended) {
    while (!*ended || descriptors[0] >= 0 || descriptors[1] >= 0) {
        if (stopping) return ECANCELED;
        if (!*ended) {
            siginfo_t status = {0};
            if (waitid(P_PID, (id_t)child, &status, WEXITED | WNOHANG | WNOWAIT) < 0) {
                if (errno == EINTR) continue;
                return errno;
            }
            if (status.si_pid == child) {
                *ended = 1;
            }
        }
        struct pollfd ready[3] = {
            {STDIN_FILENO, POLLIN, 0}, {descriptors[0], POLLIN, 0},
            {descriptors[1], POLLIN, 0}
        };
        if (poll(ready, 3, 10) < 0) {
            if (errno == EINTR) continue;
            return errno;
        }
        // The port owner sends only cancellation. Closing its pipe also stops the job.
        if (ready[0].revents) return ECANCELED;
        for (int i = 0; i < 2; ++i) {
            if (!ready[i + 1].revents) continue;
            unsigned char chunk[8192];
            ssize_t count = read(descriptors[i], chunk, sizeof(chunk));
            if (count < 0) {
                if (errno == EINTR || errno == EAGAIN) continue;
                return errno;
            }
            if (count == 0) {
                close(descriptors[i]);
                descriptors[i] = -1;
            } else {
                if (lengths[0] + lengths[1] + (size_t)count > OUTPUT_LIMIT) return EFBIG;
                memcpy(bytes[i] + lengths[i], chunk, (size_t)count);
                lengths[i] += (size_t)count;
            }
        }
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    struct sigaction action = {0};
    sigemptyset(&action.sa_mask);
    action.sa_handler = stop;
    if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL)) return 2;
    action.sa_handler = SIG_IGN;
    if (sigaction(SIGPIPE, &action, NULL)) return 2;
    action.sa_handler = SIG_DFL;
    if (sigaction(SIGCHLD, &action, NULL)) return 2;
    sigset_t empty;
    sigemptyset(&empty);
    if (sigprocmask(SIG_SETMASK, &empty, NULL)) return 2;

    int output[2] = {-1, -1}, errors[2] = {-1, -1};
    int error = make_pipe(output);
    if (!error) error = make_pipe(errors);
    pid_t child = -1;
    if (!error && !stopping) error = start(&child, argv[1], output, errors);
    if (output[1] >= 0) close(output[1]);
    if (errors[1] >= 0) close(errors[1]);

    unsigned char bytes[2][OUTPUT_LIMIT];
    size_t lengths[2] = {0, 0};
    int descriptors[2] = {output[0], errors[0]};
    int status = 0;
    if (child > 0) {
        int ended = 0;
        error = capture(child, descriptors, bytes, lengths, &ended);
        // The PID remains ours until waitpid reaps it. Never signal a reused PID.
        if (!ended && kill(child, SIGTERM) < 0 && errno != ESRCH && !error) error = errno;
        while (waitpid(child, &status, 0) < 0) {
            if (errno == EINTR) continue;
            if (!error) error = errno;
            break;
        }
    } else if (!error) {
        error = ECANCELED;
    }
    for (int i = 0; i < 2; ++i) if (descriptors[i] >= 0) close(descriptors[i]);

    if (error) {
        const char *message = error == EFBIG ? "Bash output exceeded 131072 UTF-8 bytes." : strerror(error);
        emit(4, message, strlen(message));
    } else {
        uint32_t code = htonl((uint32_t)(WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status)));
        if (!emit(1, bytes[0], lengths[0]) && !emit(2, bytes[1], lengths[1]))
            emit(3, &code, sizeof(code));
    }
    return 0;
}
