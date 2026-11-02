/* Synthetic statvfs tests compile the production functions unchanged. */
#define _GNU_SOURCE
#include <sys/statvfs.h>
#include <unistd.h>
ssize_t mocked_write(int, const void *, size_t);
int mocked_statvfs(const char *, struct statvfs *);
#define main collector_program_main
#define statvfs(path, result) mocked_statvfs(path, result)
#define write(fd, data, length) mocked_write(fd, data, length)
#include "ksync-collector.c"
#undef statvfs
#undef write
#undef main
#include <assert.h>
static int fail_write_fd = -1, short_write_pending;
ssize_t mocked_write(int fd, const void *data, size_t length) {
  if (fd == fail_write_fd) {
    if (short_write_pending) {
      short_write_pending = 0;
      return write(fd, data, length > 7 ? 7 : length);
    }
    errno = ENOSPC;
    return -1;
  }
  return write(fd, data, length);
}
static unsigned long long mocked_free = 64ULL * 1024 * 1024;
static int stat_calls, signal_fd = -1;
int mocked_statvfs(const char *path, struct statvfs *result) {
  (void)path;
  memset(result, 0, sizeof *result);
  result->f_bsize = 4096;
  result->f_frsize = 1;
  result->f_bavail = mocked_free;
  stat_calls++;
  return 0;
}
static void acknowledged_term(int signum) {
  (void)signum;
  (void)write(signal_fd, "T", 1);
  _exit(0);
}
int main(int argc, char **argv) {
  assert(argc == 2);
  snprintf(root, sizeof root, "%s", argv[1]);
  char events[PATH_CAP], file[PATH_CAP];
  join(events, root, "events");
  assert(mkdir_private(events) == 0);
  join(sealedpath, events, "sealed");
  assert(mkdir_private(sealedpath) == 0);
  join(activepath, events, "active.log");
  join(runtimepath, root, "runtime.json");
  join(checkpoint, root, "collector.checkpoint");
  join(file, root, "status.txt");
  reserve_min = 24ULL * 1024 * 1024 + 64 * 1024;
  queue_max = 4ULL * 1024 * 1024;
  /* A cached queue scan does not cache availability: a concurrent writer wins.
   */
  assert(room(100));
  mocked_free = 23ULL * 1024 * 1024;
  assert(!room(100));
  assert(blocked);
  blocked = 0;
  /* Status reporting uses a fixed system floor, not a deliberately huge CLI
   * capture reserve, so low-space failure metadata can still be written. */
  reserve_min = ULLONG_MAX;
  assert(atomic_text(file, "old") == 0);
  assert(!blocked);
  /* Block rounding is applied BEFORE creating any new temporary file. */
  mocked_free = 20ULL * 1024 * 1024 + 64 * 1024 + 4095;
  assert(atomic_text(file, "new") == -1);
  assert(blocked);
  char data[8] = {0};
  FILE *held = fopen(file, "r");
  assert(held);
  assert(fread(data, 1, sizeof data, held) == 3);
  fclose(held);
  assert(!strcmp(data, "old"));
  char tmp[PATH_CAP];
  assert(snprintf(tmp, sizeof tmp, "%s.tmp-%ld", file, (long)getpid()) > 0);
  assert(access(tmp, F_OK) == -1);
  /* A genuine short append followed by ENOSPC must not corrupt the next
   * replayed raw line; failed rollback is quarantined instead of sealed. */
  blocked = 0;
  mocked_free = 64ULL * 1024 * 1024;
  reserve_min = 24ULL * 1024 * 1024 + 64 * 1024;
  assert(active_open() == 0);
  assert(write(activefd, "sentinel\n", 9) == 9);
  fail_write_fd = activefd;
  short_write_pending = 1;
  const char *line =
      "261101:220000 cvm: I "
      "ReadingTimerController:Information::OpenBook,StoredBookData:null;\n";
  assert(process_line(line, strlen(line)) == -1);
  assert(blocked);
  assert(!active_unsafe);
  fail_write_fd = -1;
  struct stat event_stat;
  assert(fstat(activefd, &event_stat) == 0 && event_stat.st_size == 9);
  close(activefd);
  activefd = -1;
  /* A large normal checkpoint cannot consume the worker rollback scratch,
   * while the small emergency marker remains writable under its fixed floor. */
  blocked = 0;
  mocked_free = 24ULL * 1024 * 1024 + 100 * 1024;
  stat_calls = 0;
  nc = 40;
  for (int i = 0; i < nc; i++) {
    memset(curs[i].path, 'x', 4000);
    curs[i].path[0] = '/';
    curs[i].path[4000] = 0;
    curs[i].ino = (unsigned long long)i + 1;
  }
  assert(save() == -1);
  assert(blocked);
  assert(stat_calls == 1);
  assert(access(checkpoint, F_OK) == -1);
  runtime();
  assert(access(runtimepath, F_OK) == 0);
  assert(stat_calls == 2);
  nc = 0;
  memset(curs, 0, sizeof curs);
  /* Below the 20 MiB floor, fail closed and cancel a real fake worker without
   * recursively trying to write runtime (and without any network code). */
  int channel[2];
  assert(pipe(channel) == 0);
  worker = fork();
  assert(worker >= 0);
  if (!worker) {
    close(channel[0]);
    signal_fd = channel[1];
    signal(SIGTERM, acknowledged_term);
    assert(write(signal_fd, "R", 1) == 1);
    for (;;)
      pause();
  }
  close(channel[1]);
  assert(read(channel[0], data, 1) == 1 && data[0] == 'R');
  blocked = 0;
  mocked_free = 19ULL * 1024 * 1024;
  stat_calls = 0;
  assert(atomic_text(file, "denied") == -1);
  assert(blocked);
  assert(stat_calls == 1);
  struct pollfd ready = {channel[0], POLLIN, 0};
  assert(poll(&ready, 1, 2000) > 0);
  assert(read(channel[0], data, 1) == 1 && data[0] == 'T');
  int status;
  assert(waitpid(worker, &status, 0) == worker);
  assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
  worker = 0;
  close(channel[0]);
  puts("fresh availability, block rounding, metadata floor and cancellation: "
       "passed");
  return 0;
}
