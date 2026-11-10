#define _POSIX_C_SOURCE 200809L
#include <erl_nif.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* Only descriptor and signal ownership lives here. Framing and scheduling are Elixir. */
typedef struct {
    ErlNifMutex *lock;
    ErlNifPid owner;
    ErlNifMonitor monitor;
    int fd;
    int flags;
    int writing;
    int signal;
    struct sigaction previous;
} Handle;

static ErlNifResourceType *handle_type;
static ErlNifMutex *signal_lock;
static int signal_claimed;
static _Atomic int interrupted;
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "Signal flag must be lock-free");

static ERL_NIF_TERM atom(ErlNifEnv *env, const char *name) {
    return enif_make_atom(env, name);
}

static ERL_NIF_TERM error(ErlNifEnv *env, const char *message) {
    ERL_NIF_TERM text;
    size_t size = strlen(message);
    unsigned char *bytes = enif_make_new_binary(env, size, &text);
    if (!bytes) return enif_raise_exception(env, atom(env, "enomem"));
    memcpy(bytes, message, size);
    return enif_make_tuple2(env, atom(env, "error"), text);
}

/* Caller holds the resource lock. Repeated close is harmless. */
static int close_handle(Handle *handle) {
    int failed = 0;
    if (handle->signal) {
        enif_mutex_lock(signal_lock);
        if (sigaction(SIGINT, &handle->previous, NULL) == -1) failed = errno;
        signal_claimed = 0;
        handle->signal = 0;
        enif_mutex_unlock(signal_lock);
    }
    if (handle->fd >= 0) {
        if (fcntl(handle->fd, F_SETFL, handle->flags) == -1) failed = errno;
        if (close(handle->fd) == -1 && failed == 0) failed = errno;
        handle->fd = -1;
    }
    return failed;
}

static void destroy(ErlNifEnv *env, void *resource) {
    (void)env;
    Handle *handle = resource;
    if (handle->lock) {
        enif_mutex_lock(handle->lock);
        close_handle(handle);
        enif_mutex_unlock(handle->lock);
        enif_mutex_destroy(handle->lock);
    }
}

static void owner_down(ErlNifEnv *env, void *resource, ErlNifPid *pid, ErlNifMonitor *monitor) {
    (void)env;
    (void)pid;
    (void)monitor;
    Handle *handle = resource;
    enif_mutex_lock(handle->lock);
    close_handle(handle);
    enif_mutex_unlock(handle->lock);
}

static Handle *allocate(ErlNifEnv *env) {
    Handle *handle = enif_alloc_resource(handle_type, sizeof(Handle));
    if (!handle) return NULL;
    memset(handle, 0, sizeof(*handle));
    handle->fd = -1;
    handle->lock = enif_mutex_create("tinyagent_rpc_io");
    if (!handle->lock || !enif_self(env, &handle->owner) ||
        enif_monitor_process(env, handle, &handle->owner, &handle->monitor) != 0) {
        enif_release_resource(handle);
        return NULL;
    }
    return handle;
}

static Handle *owned(ErlNifEnv *env, ERL_NIF_TERM value) {
    Handle *handle;
    ErlNifPid caller;
    if (!enif_get_resource(env, value, handle_type, (void **)&handle) ||
        !enif_self(env, &caller) || enif_compare_pids(&caller, &handle->owner) != 0) return NULL;
    return handle;
}

static ERL_NIF_TERM open_fd(ErlNifEnv *env, int argc, const ERL_NIF_TERM argv[]) {
    (void)argc;
    int fd, writing;
    struct stat info;
    if (!enif_get_int(env, argv[0], &fd) || fd < 0 ||
        !enif_get_int(env, argv[1], &writing) || (writing != 0 && writing != 1))
        return enif_make_badarg(env);
    if (fstat(fd, &info) == -1) return error(env, strerror(errno));
    if (!S_ISFIFO(info.st_mode) && !S_ISREG(info.st_mode) && !S_ISCHR(info.st_mode))
        return error(env, "RPC requires a pipe, file, or terminal descriptor");
    int flags = fcntl(fd, F_GETFL);
    if (flags == -1) return error(env, strerror(errno));
    int access = flags & O_ACCMODE;
    if ((writing && access == O_RDONLY) || (!writing && access == O_WRONLY))
        return error(env, "RPC descriptor has the wrong access mode");
    Handle *handle = allocate(env);
    if (!handle) return error(env, "Cannot allocate RPC descriptor");
    handle->flags = flags;
    handle->writing = writing;
    handle->fd = fcntl(fd, F_DUPFD_CLOEXEC, 3);
    if (handle->fd == -1 || fcntl(handle->fd, F_SETFL, flags | O_NONBLOCK) == -1) {
        int failed = errno;
        enif_release_resource(handle);
        return error(env, strerror(failed));
    }
    ERL_NIF_TERM value = enif_make_resource(env, handle);
    enif_release_resource(handle);
    return enif_make_tuple2(env, atom(env, "ok"), value);
}

static ERL_NIF_TERM read_fd(ErlNifEnv *env, int argc, const ERL_NIF_TERM argv[]) {
    (void)argc;
    Handle *handle = owned(env, argv[0]);
    if (!handle) return enif_make_badarg(env);
    unsigned char bytes[8192];
    enif_mutex_lock(handle->lock);
    if (handle->fd < 0 || handle->writing) {
        enif_mutex_unlock(handle->lock);
        return error(env, "RPC input is closed");
    }
    ssize_t count = read(handle->fd, bytes, sizeof(bytes));
    int failed = errno;
    enif_mutex_unlock(handle->lock);
    if (count == 0) return atom(env, "eof");
    if (count < 0) {
        if (failed == EINTR || failed == EAGAIN || failed == EWOULDBLOCK) return atom(env, "again");
        return error(env, strerror(failed));
    }
    ERL_NIF_TERM value;
    unsigned char *result = enif_make_new_binary(env, (size_t)count, &value);
    if (!result) return enif_raise_exception(env, atom(env, "enomem"));
    memcpy(result, bytes, (size_t)count);
    return enif_make_tuple2(env, atom(env, "ok"), value);
}

/* A broken pipe must not terminate the VM or consume an already pending SIGPIPE. */
static ssize_t write_without_sigpipe(int fd, const void *bytes, size_t size) {
    sigset_t blocked, previous, pending;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGPIPE);
    int result = pthread_sigmask(SIG_BLOCK, &blocked, &previous);
    if (result != 0) { errno = result; return -1; }
    sigpending(&pending);
    int was_pending = sigismember(&pending, SIGPIPE);
    ssize_t written = write(fd, bytes, size);
    int failed = errno;
    if (written < 0 && failed == EPIPE && !was_pending) {
        sigpending(&pending);
        if (sigismember(&pending, SIGPIPE)) {
            int signal;
            sigwait(&blocked, &signal);
        }
    }
    pthread_sigmask(SIG_SETMASK, &previous, NULL);
    errno = failed;
    return written;
}

static ERL_NIF_TERM write_fd(ErlNifEnv *env, int argc, const ERL_NIF_TERM argv[]) {
    (void)argc;
    Handle *handle = owned(env, argv[0]);
    ErlNifBinary bytes;
    unsigned long offset;
    if (!handle || !enif_inspect_binary(env, argv[1], &bytes) ||
        !enif_get_ulong(env, argv[2], &offset) || offset >= bytes.size) return enif_make_badarg(env);
    size_t size = bytes.size - offset;
    if (size > 65536) size = 65536;
    enif_mutex_lock(handle->lock);
    if (handle->fd < 0 || !handle->writing) {
        enif_mutex_unlock(handle->lock);
        return error(env, "RPC output is closed");
    }
    ssize_t written = write_without_sigpipe(handle->fd, bytes.data + offset, size);
    int failed = errno;
    enif_mutex_unlock(handle->lock);
    if (written < 0) {
        if (failed == EINTR || failed == EAGAIN || failed == EWOULDBLOCK) return atom(env, "again");
        return error(env, strerror(failed));
    }
    if (written == 0) return error(env, "RPC output made no progress");
    return enif_make_tuple2(env, atom(env, "ok"), enif_make_long(env, written));
}

static void on_interrupt(int signal) {
    (void)signal;
    atomic_store_explicit(&interrupted, 1, memory_order_relaxed);
}

static ERL_NIF_TERM watch_interrupt(ErlNifEnv *env, int argc, const ERL_NIF_TERM argv[]) {
    (void)argc;
    (void)argv;
    Handle *handle = allocate(env);
    if (!handle) return error(env, "Cannot allocate RPC signal owner");
    enif_mutex_lock(signal_lock);
    if (signal_claimed) {
        enif_mutex_unlock(signal_lock);
        enif_release_resource(handle);
        return error(env, "An RPC connection already owns SIGINT");
    }
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = on_interrupt;
    sigemptyset(&action.sa_mask);
    atomic_store_explicit(&interrupted, 0, memory_order_relaxed);
    if (sigaction(SIGINT, &action, &handle->previous) == -1) {
        int failed = errno;
        enif_mutex_unlock(signal_lock);
        enif_release_resource(handle);
        return error(env, strerror(failed));
    }
    handle->signal = 1;
    signal_claimed = 1;
    enif_mutex_unlock(signal_lock);
    ERL_NIF_TERM value = enif_make_resource(env, handle);
    enif_release_resource(handle);
    return enif_make_tuple2(env, atom(env, "ok"), value);
}

static ERL_NIF_TERM interrupted_signal(ErlNifEnv *env, int argc, const ERL_NIF_TERM argv[]) {
    (void)argc;
    Handle *handle = owned(env, argv[0]);
    if (!handle || !handle->signal) return enif_make_badarg(env);
    return atom(env, atomic_load_explicit(&interrupted, memory_order_relaxed) ? "true" : "false");
}

static ERL_NIF_TERM close_resource(ErlNifEnv *env, int argc, const ERL_NIF_TERM argv[]) {
    (void)argc;
    Handle *handle = owned(env, argv[0]);
    if (!handle) return enif_make_badarg(env);
    enif_mutex_lock(handle->lock);
    int failed = close_handle(handle);
    enif_mutex_unlock(handle->lock);
    return failed ? error(env, strerror(failed)) : atom(env, "ok");
}

static int load(ErlNifEnv *env, void **data, ERL_NIF_TERM info) {
    (void)data;
    (void)info;
    /* Hot upgrades require an explicit resource takeover; reject them safely. */
    if (handle_type) return -1;
    ErlNifResourceTypeInit callbacks = { .dtor = destroy, .down = owner_down };
    signal_lock = enif_mutex_create("tinyagent_rpc_sigint");
    if (!signal_lock) return -1;
    handle_type = enif_open_resource_type_x(env, "rpc_io", &callbacks, ERL_NIF_RT_CREATE, NULL);
    if (!handle_type) { enif_mutex_destroy(signal_lock); return -1; }
    return 0;
}

static void unload(ErlNifEnv *env, void *data) {
    (void)env;
    (void)data;
    enif_mutex_destroy(signal_lock);
    signal_lock = NULL;
    handle_type = NULL;
}

static ErlNifFunc functions[] = {
    {"open", 2, open_fd, ERL_NIF_DIRTY_JOB_IO_BOUND},
    {"read", 1, read_fd, ERL_NIF_DIRTY_JOB_IO_BOUND},
    {"write", 3, write_fd, ERL_NIF_DIRTY_JOB_IO_BOUND},
    {"watch_interrupt", 0, watch_interrupt, 0},
    {"interrupted?", 1, interrupted_signal, 0},
    {"close", 1, close_resource, ERL_NIF_DIRTY_JOB_IO_BOUND}
};
ERL_NIF_INIT(Elixir.TinyAgent.RpcIO, functions, load, NULL, NULL, unload)
